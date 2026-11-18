"""Observed crossings and wait estimates for a directed boarding stop."""
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging import getLogger
from re import fullmatch
from statistics import median

from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError

from backend.integrations.cheonan_timetables import BASE, LOOKUP_ERRORS, SEOUL, service_day_type
from backend.models.tables import BusPassage

POSITIONS_URL = f'{BASE}/getRealtimeBusLocPopList.do'
LOOKBACK = timedelta(days=42)
SAMPLE_TTL = timedelta(seconds=15)
MAX_CROSSING_GAP = timedelta(seconds=75)
HISTORY_WINDOW = timedelta(minutes=60)
LOG = getLogger(__name__)


@dataclass(frozen=True)
class Boarding:
    route_id: str
    station_id: str
    direction: str
    sequence: int

    @property
    def arrival_key(self):
        return self.route_id, self.station_id


@dataclass(frozen=True)
class Position:
    sequence: int
    section_group: str


@dataclass(frozen=True)
class PositionSnapshot:
    observed_at: datetime
    vehicles: dict[str, Position]
    cancelled: frozenset[str]
    served_sequences: frozenset[int]


def vehicle_id(row):
    value = str(row.get('VEH_ID') or '').strip()
    return value if value.isdecimal() and value not in ('0', '9') else None


def operation(row):
    value = str(row.get('OPERATION_STATUS') or row.get('operationStatus') or '').strip().casefold()
    if value in ('cancelled', 'canceled', '결행', '운행 취소'):
        return 'cancelled'
    if value in ('not_running', 'ended', '운행 종료', '운행종료', '운행없음'):
        return 'not_running'
    return None


def parse_positions(rows, route_id, direction, stops, now):
    if not isinstance(rows, list) or len(rows) < len(stops):
        return None
    positions = {}
    ambiguous = set()
    cancelled = set()
    covered = set()
    served_sequences = set()
    for row in rows:
        if not isinstance(row, dict) or str(row.get('ROUTE_ID')) != route_id:
            return None
        row_direction = str(row.get('ROUTE_DIRECTION') or '').strip()
        if row_direction and row_direction != direction:
            return None
        order = str(row.get('STOP_ORD') or '')
        if not order.isdecimal() or not 1 <= int(order) <= len(stops):
            return None
        sequence = int(order)
        if str(row.get('STOP_ID') or row.get('NODE_ID')) != stops[sequence - 1]:
            return None
        covered.add(sequence)
        if str(row.get('PASSSTOP_DISP_FLAG') or '0') == '0':
            served_sequences.add(sequence)
        vehicle = vehicle_id(row)
        if vehicle is None:
            if str(row.get('VEH_ID')) != '9':
                return None
            continue
        if operation(row) == 'cancelled':
            cancelled.add(vehicle)
            continue
        position = Position(sequence, str(row.get('ROUTESECT_GROUP_ID') or '').strip())
        if vehicle in positions and positions[vehicle] != position:
            ambiguous.add(vehicle)
        positions[vehicle] = position
    if len(covered) != len(stops) or ambiguous - cancelled:
        return None
    return PositionSnapshot(now, {v: p for v, p in positions.items() if v not in cancelled}, frozenset(cancelled),
                            frozenset(served_sequences))


def observed_crossings(previous, current, stops):
    if previous is None or not timedelta(0) < current.observed_at - previous.observed_at <= MAX_CROSSING_GAP:
        return []
    crossings = []
    for vehicle, position in current.vehicles.items():
        before = previous.vehicles.get(vehicle)
        if before is None or not before.section_group or before.section_group != position.section_group:
            continue
        for stop in stops:
            # The two ordered observations bracket the actual pass. Use the
            # observation after the pass, never a predicted arrival timestamp.
            if (stop.sequence in previous.served_sequences and stop.sequence in current.served_sequences
                    and before.sequence in (stop.sequence - 1, stop.sequence) and position.sequence == stop.sequence + 1):
                crossings.append({'route_id':stop.route_id, 'station_id':stop.station_id, 'direction':stop.direction,
                                  'sequence':stop.sequence, 'vehicle_id':vehicle, 'passed_at':current.observed_at.timestamp()})
    return crossings


class CheonanArrivalHistory:
    def __init__(self, database, transport):
        self.database = database
        self.transport = transport
        self.recent = {}
        self.positions = {}
        self.locks = defaultdict(asyncio.Lock)
        self.slots = asyncio.Semaphore(6)

    async def sample(self, http, direction, stops, now):
        route_id = stops[0].route_id.removeprefix('cheonan-route-')
        key = (route_id, direction)
        try:
            async with self.locks[key]:
                previous = self.positions.get(key)
                if previous and now - previous.observed_at < SAMPLE_TTL:
                    return previous if previous.observed_at <= now else None
                async with self.slots:
                    response = await http.get(POSITIONS_URL, params={'routeId':route_id})
                    response.raise_for_status()
                snapshot = parse_positions(response.json()['resultList'], route_id, direction,
                                           self.transport.routes[route_id]['stops'], now)
                if snapshot is not None:
                    self.positions[key] = snapshot
                    crossings = observed_crossings(previous, snapshot, stops)
                    if crossings:
                        await asyncio.to_thread(self.record, crossings, now)
                return snapshot
        except LOOKUP_ERRORS:
            return None

    def record(self, crossings, now):
        try:
            with self.database.session() as db:
                db.execute(insert(BusPassage).on_conflict_do_nothing(), crossings)
                db.execute(delete(BusPassage).where(BusPassage.passed_at < (now - LOOKBACK).timestamp()))
        except SQLAlchemyError:
            LOG.warning('Unable to store confirmed Cheonan bus passages', exc_info=True)

    def passages(self, stops, now):
        passages = defaultdict(list)
        keys = [(stop.route_id, stop.direction, stop.station_id, stop.sequence) for stop in stops]
        if not keys:
            return passages
        try:
            with self.database.session() as db:
                rows = db.scalars(select(BusPassage).where(
                    tuple_(BusPassage.route_id, BusPassage.direction, BusPassage.station_id, BusPassage.sequence).in_(keys),
                    BusPassage.passed_at.between((now - LOOKBACK).timestamp(), now.timestamp()),
                ).order_by(BusPassage.passed_at))
                for row in rows:
                    stop = Boarding(row.route_id, row.station_id, row.direction, row.sequence)
                    passages[stop].append(datetime.fromtimestamp(row.passed_at, timezone.utc).astimezone(SEOUL))
        except SQLAlchemyError:
            LOG.warning('Unable to read confirmed Cheonan bus passages', exc_info=True)
        return passages


def next_by_headway(passages, metadata, now):
    if not passages:
        return None
    last = passages[-1]
    if last.astimezone(SEOUL).date() != now.astimezone(SEOUL).date():
        return None
    fixed = fullmatch(r'\s*(\d{1,3})\s*분(?:\s*간격)?\s*', str(metadata.get('ALLOC_TIME') or ''))
    interval = int(fixed[1]) * 60 if fixed else 0
    if not interval:
        recent = [stamp for stamp in passages if last - stamp <= HISTORY_WINDOW]
        gaps = [(second - first).total_seconds() for first, second in zip(recent, recent[1:])]
        if len(gaps) < 2 or min(gaps) < 60 or max(gaps) > 2 * min(gaps):
            return None
        interval = median(gaps)
    expected = last + timedelta(seconds=interval)
    return expected if expected > now else None


def historical_wait(passages, now):
    """Use days with observations on both sides of the same local clock time."""
    local = now.astimezone(SEOUL)
    weekend = service_day_type(local.date()) != 'weekday'
    by_day = defaultdict(list)
    for passed in passages:
        day = passed.date()
        if day < local.date() and (service_day_type(day) != 'weekday') == weekend:
            by_day[day].append(passed)
    waits = []
    for day, times in by_day.items():
        reference = datetime.combine(day, local.timetz())
        if not any(reference - HISTORY_WINDOW <= passed < reference for passed in times):
            continue
        following = next((passed for passed in times if reference < passed <= reference + HISTORY_WINDOW), None)
        if following is not None:
            waits.append(following - reference)
    if len(waits) < 3:
        return None
    waits.sort()
    last_index = len(waits) - 1
    return local + waits[last_index // 4], local + waits[(3 * last_index + 3) // 4]
