"""Resolve Cheonan arrivals from live data, schedules, and confirmed passes."""
import asyncio
from collections import defaultdict
from dataclasses import replace
from datetime import timedelta
from math import isfinite

import httpx

from backend.integrations.cheonan_history import Boarding, historical_wait, next_by_headway, operation, vehicle_id
from backend.integrations.cheonan_timetables import BASE, LOOKUP_ERRORS
from backend.integrations.providers import ArrivalObservation, ProviderState
from backend.models.schemas import ServiceChanges
from backend.services.routing import change_period
from backend.services.time import freshness, quality

URL = f'{BASE}/getArrivalScheduleList.do'
LIVE = 'cheonan-bis-arrival'
UPSTREAM = 'cheonan-bis-upstream'
RECENT = 'cheonan-bis-recent'
HEADWAY = 'cheonan-bis-headway'
HISTORY = 'cheonan-bis-history'
OPERATION = 'cheonan-bis-operation'


def public_route_id(route_id):
    return route_id.removeprefix('cheonan-route-')


def matching_rows(rows, boarding):
    route_id = public_route_id(boarding.route_id)
    for row in rows:
        if str(row.get('ROUTE_ID')) != route_id:
            continue
        direction = str(row.get('ROUTE_DIRECTION') or '').strip()
        # Each BIS ROUTE_ID identifies a single direction. Reject a contradictory direction if supplied.
        if not boarding.direction or not direction or direction == boarding.direction:
            yield row


def valid_minutes(row):
    value = row.get('RTIME')
    return type(value) in (int, float) and isfinite(value) and 0 <= value <= 180


def direct_arrival(rows, now, cancelled):
    row = min((row for row in rows if valid_minutes(row) and not operation(row)
               and vehicle_id(row) not in cancelled), key=lambda row: row['RTIME'], default=None)
    if row is None:
        return None
    return ArrivalObservation(now + timedelta(minutes=row['RTIME']), fetched_at=now, source_id=LIVE,
                              vehicle_id=vehicle_id(row))


def recent_arrival(observation, now, cancelled):
    if (observation is None or observation.vehicle_id in cancelled or observation.expected_arrival_at is None
            or observation.expected_arrival_at <= now
            or freshness(observation.source_updated_at or observation.fetched_at, now) != 'fresh'):
        return None
    return replace(observation, source_id=RECENT)


def stopped(now):
    return ArrivalObservation(None, operation_status='not_running', source_updated_at=now, fetched_at=now,
                              source_id=OPERATION)


def cancelled_route(provider, boarding, now):
    return provider and any(change.type == 'cancellation' and change.dataQuality.freshness == 'fresh'
        and change_period(change, now) == 'active' and boarding.route_id in change.affectedRouteIds
        and (not change.affectedStationIds or boarding.station_id in change.affectedStationIds)
        for change in provider.changes.items)


async def bounded(seconds, tasks):
    try:
        async with asyncio.timeout(seconds):
            await asyncio.gather(*tasks)
    except TimeoutError:
        pass


async def arrival_state(detail, now, *, client=None, timetables=None, history=None, provider=None):
    return await arrival_state_many([detail], now, client=client, timetables=timetables, history=history, provider=provider)


async def arrival_state_many(details, now, *, client=None, timetables=None, history=None, provider=None):
    if client is None:
        async with httpx.AsyncClient(timeout=2, headers={'User-Agent': 'CheonanSmartBus/0.1 arrival-query'}) as http:
            return await collect_arrivals(details, now, http, timetables, history, provider)
    return await collect_arrivals(details, now, client, timetables, history, provider)


async def collect_arrivals(details, now, http, timetables, history, provider):
    buses = {(segment.routeId, segment.boardStation.stationId): segment for detail in details
             for segment in detail.segments if segment.type == 'bus'}
    observations, rows_by_stop, metadata = {}, {}, {}
    slots = asyncio.Semaphore(6)

    async def load_stop(station_id):
        if station_id in rows_by_stop:
            return
        try:
            async with slots:
                response = await http.get(URL, params={'stopId':station_id.removeprefix('cheonan-stop-')})
                response.raise_for_status()
            rows = response.json()['resultList']
            if isinstance(rows, list):
                rows_by_stop[station_id] = [row for row in rows if isinstance(row, dict)]
        except LOOKUP_ERRORS:
            pass

    async def load_metadata():
        try:
            metadata.update(await timetables.load_routes(http))
        except LOOKUP_ERRORS:
            pass

    initial = [load_stop(station_id) for station_id in {station_id for _, station_id in buses}]
    if history is not None and timetables is not None and buses:
        initial.append(load_metadata())
    await bounded(3, initial)

    boardings = []
    for (route_id, station_id), segment in buses.items():
        raw_route_id = public_route_id(route_id)
        direction = str(metadata.get(raw_route_id, {}).get('ROUTE_DIRECTION') or '').strip()
        if history is not None:
            route = history.transport.routes.get(raw_route_id)
            route_stops = route['stops'] if route else []
            if not 1 <= segment.boardSequence < len(route_stops):
                continue
            if route_stops[segment.boardSequence - 1] != station_id.removeprefix('cheonan-stop-'):
                continue
        boardings.append(Boarding(route_id, station_id, direction, segment.boardSequence))

    positions = {}
    by_route = defaultdict(list)
    for boarding in boardings:
        if boarding.direction:
            by_route[boarding.route_id].append(boarding)

    async def sample_positions(route_id, stops):
        snapshot = await history.sample(http, stops[0].direction, stops, now)
        if snapshot is not None:
            positions[route_id] = snapshot

    await bounded(3, [sample_positions(route_id, stops) for route_id, stops in by_route.items()])

    # A confirmed cancellation applies to the vehicle wherever it appears in this refresh.
    cancelled = defaultdict(set)
    for route_id, snapshot in positions.items():
        cancelled[route_id].update(snapshot.cancelled)
    for boarding in boardings:
        cancelled[boarding.route_id].update(vehicle_id(row) for row in matching_rows(rows_by_stop.get(boarding.station_id, ()), boarding)
                                             if operation(row) == 'cancelled' and vehicle_id(row) is not None)

    for boarding in boardings:
        key = boarding.arrival_key
        rows = list(matching_rows(rows_by_stop.get(boarding.station_id, ()), boarding))
        catalogue = metadata.get(public_route_id(boarding.route_id), {})
        confirmed = provider.arrivals.get(key) if provider else None
        confirmed_ended = (confirmed and confirmed.operation_status == 'not_running'
                           and freshness(confirmed.source_updated_at or confirmed.fetched_at, now) == 'fresh')
        if (confirmed_ended or operation(catalogue) == 'not_running' or catalogue.get('ALLOC_TIME') == '운행없음'
                or any(operation(row) == 'not_running' and vehicle_id(row) is None for row in rows)):
            observations[key] = stopped(now)
        elif cancelled_route(provider, boarding, now):
            observations[key] = ArrivalObservation(None)
        else:
            observation = direct_arrival(rows, now, cancelled[boarding.route_id])
            if observation is not None:
                observations[key] = observation
        if history is not None:
            previous = history.recent.get(boarding)
            if previous and (previous.vehicle_id in cancelled[boarding.route_id] or
                             observations.get(key) is not None and observations[key].source_id != LIVE):
                history.recent.pop(boarding, None)
            if observations.get(key) and observations[key].source_id == LIVE:
                if previous is None or previous.fetched_at <= now:
                    history.recent[boarding] = observations[key]

    if history is not None:
        # Stage 1. Require a live vehicle position on the same section group,
        # before the upstream stop, and an unambiguous path to the boarding stop.
        upstream = {}
        for boarding in boardings:
            snapshot = positions.get(boarding.route_id)
            if (boarding.arrival_key in observations or snapshot is None or not snapshot.vehicles
                    or boarding.sequence not in snapshot.served_sequences):
                continue
            route_stops = history.transport.routes[public_route_id(boarding.route_id)]['stops']
            if route_stops.count(route_stops[boarding.sequence - 1]) != 1:
                continue
            candidates = {boarding.sequence - 1, boarding.sequence - 2}
            for position in snapshot.vehicles.values():
                candidates.update((position.sequence, position.sequence + 1))
            upstream[boarding] = [(sequence, 'cheonan-stop-' + route_stops[sequence - 1])
                                  for sequence in sorted(candidates, reverse=True)
                                  if 1 <= sequence < boarding.sequence and sequence in snapshot.served_sequences
                                  and route_stops.count(route_stops[sequence - 1]) == 1]
        upstream_stations = dict.fromkeys(sid for stops in upstream.values() for _, sid in stops)
        await bounded(3, [load_stop(station_id) for station_id in upstream_stations])
        for boarding, stops in upstream.items():
            for _, station_id in stops:
                cancelled[boarding.route_id].update(vehicle_id(row)
                    for row in matching_rows(rows_by_stop.get(station_id, ()), boarding)
                    if operation(row) == 'cancelled' and vehicle_id(row) is not None)
        for boarding in boardings:
            existing = observations.get(boarding.arrival_key)
            if existing and existing.vehicle_id in cancelled[boarding.route_id]:
                observations.pop(boarding.arrival_key, None)
        for boarding, stops in upstream.items():
            vehicles = positions[boarding.route_id].vehicles
            best = None
            for sequence, station_id in stops:
                rows = list(matching_rows(rows_by_stop.get(station_id, ()), boarding))
                estimate = await upstream_arrival(history.transport, boarding, sequence, rows, vehicles,
                                                  cancelled[boarding.route_id], now)
                if estimate and (best is None or estimate.expected_arrival_at < best.expected_arrival_at):
                    best = estimate
            if best and best.vehicle_id not in cancelled[boarding.route_id]:
                observations[boarding.arrival_key] = best

        # Stage 2. A cached response keeps its original timestamp.
        for boarding in boardings:
            if boarding.arrival_key not in observations:
                previous = history.recent.get(boarding)
                if previous and previous.vehicle_id in cancelled[boarding.route_id]:
                    history.recent.pop(boarding, None)
                observation = recent_arrival(previous, now, cancelled[boarding.route_id])
                if observation is not None:
                    observations[boarding.arrival_key] = observation

    # Stage 3.
    if timetables is not None:
        idle_routes = {public_route_id(route_id) for route_id, snapshot in positions.items() if not snapshot.vehicles}
        end_check_keys = {key for key, observation in observations.items()
                          if observation.source_id == RECENT and public_route_id(key[0]) in idle_routes}
        await timetables.collect(http, details, observations, now, idle_routes=idle_routes, end_check_keys=end_check_keys)
        if history is not None:
            for boarding in boardings:
                if boarding.arrival_key in end_check_keys and observations[boarding.arrival_key].operation_status == 'not_running':
                    history.recent.pop(boarding, None)

    if history is not None:
        remaining = [boarding for boarding in boardings if boarding.direction and boarding.arrival_key not in observations]
        passages = await asyncio.to_thread(history.passages, remaining, now)
        for boarding in remaining:
            times = passages.get(boarding, ())
            # Stages 4 and 5.
            expected = next_by_headway(times, metadata.get(public_route_id(boarding.route_id), {}), now)
            if expected is not None:
                observations[boarding.arrival_key] = ArrivalObservation(expected, fetched_at=now, source_id=HEADWAY)
            elif interval := historical_wait(times, now):
                observations[boarding.arrival_key] = ArrivalObservation(interval[0], fetched_at=now, source_id=HISTORY,
                                                                       range_end_at=interval[1])

    changes = provider.changes if provider else ServiceChanges(coverageStatus='unknown', items=[], dataQuality=quality(None, now, now, LIVE))
    available = any(observation.expected_arrival_at is not None for observation in observations.values())
    return ProviderState(True, available, None, now, changes, observations)


async def upstream_arrival(transport, boarding, sequence, rows, vehicles, cancelled, now):
    eligible = []
    for row in rows:
        vehicle = vehicle_id(row)
        position = vehicles.get(vehicle)
        group = str(row.get('ROUTESECT_GROUP_ID') or '').strip()
        if (position and group and group == position.section_group and position.sequence <= sequence
                and vehicle not in cancelled and not operation(row) and valid_minutes(row)):
            eligible.append(row)
    if not eligible:
        return None
    row = min(eligible, key=lambda row: row['RTIME'])
    segment = await asyncio.to_thread(transport.bus, public_route_id(boarding.route_id), sequence - 1,
                                      boarding.sequence - 1, 'upstream-estimate')
    if not segment.durationSeconds:
        return None
    return ArrivalObservation(now + timedelta(minutes=row['RTIME'], seconds=segment.durationSeconds),
                              fetched_at=now, source_id=UPSTREAM, vehicle_id=vehicle_id(row))
