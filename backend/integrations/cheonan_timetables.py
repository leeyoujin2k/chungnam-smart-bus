"""Cheonan BIS departures and scheduled arrival estimates at the kiosk."""
import asyncio
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from re import DOTALL, findall
from time import monotonic
from urllib.parse import quote

import httpx
from holidays import KR

from backend.integrations.providers import ArrivalObservation

BASE = 'https://its.cheonan.go.kr/bis'
SOURCE = 'cheonan-bis-timetable'
ESTIMATE_SOURCE = 'cheonan-bis-timetable-estimate'
SEOUL = timezone(timedelta(hours=9))
CACHE_SECONDS = 900
PUBLIC_HOLIDAYS = KR()
DAY_TYPES = {'평일': 'weekday', '토요일': 'saturday', '주말': 'saturday', '공휴일': 'holiday'}
LOOKUP_ERRORS = (httpx.HTTPError, ValueError, TypeError, KeyError, TimeoutError)


def parse_departures(html):
    departures = {}
    for heading, body in findall(r'<dt>\s*(평일|주말|토요일|공휴일)\s+출발시간\s*</dt>(.*?)</dl>', html, DOTALL):
        times = []
        day_offset = 0
        for hour, minute in findall(r'<dd>\s*(\d{1,2}):([0-5]\d)\s*</dd>', body):
            hour, minute = int(hour), int(minute)
            if hour > 47:
                continue
            if hour < 6 and times and times[-1] >= 18 * 60:
                day_offset = 24 * 60
            times.append(hour * 60 + minute + (day_offset if hour < 24 else 0))
        departures[DAY_TYPES[heading]] = tuple(sorted(set(times)))
    return departures if departures.keys() == set(DAY_TYPES.values()) else None


def service_day_type(day):
    if day.weekday() == 6 or day in PUBLIC_HOLIDAYS:
        return 'holiday'
    return 'saturday' if day.weekday() == 5 else 'weekday'


def scheduled_arrivals(departures, now, travel_seconds):
    local = now.astimezone(SEOUL)
    for service_day in (local.date() - timedelta(days=1), local.date()):
        midnight = datetime.combine(service_day, time.min, SEOUL)
        for minute in departures[service_day_type(service_day)]:
            yield midnight + timedelta(minutes=minute, seconds=travel_seconds)


def next_departure(departures, now, travel_seconds=0):
    return min((value for value in scheduled_arrivals(departures, now, travel_seconds) if value >= now), default=None)


def schedule_key(row):
    return tuple(str(row.get(field) or '').strip() for field in ('ROUTE_NAME', 'ROUTE_DIRECTION', 'RELAY_AREACODE'))


class CheonanTimetables:
    def __init__(self, transport):
        self.transport = transport
        self.routes = {}
        self.ambiguous = set()
        self.routes_expire_at = 0
        self.routes_lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(6)
        self.tables = {}

    async def load_routes(self, http):
        async with self.routes_lock:
            if monotonic() < self.routes_expire_at:
                return self.routes
            response = await http.get(f'{BASE}/getRouteList.do', params={'searchKeyword': ''})
            response.raise_for_status()
            rows = response.json()['resultList']
            if not isinstance(rows, list):
                raise ValueError('Invalid Cheonan route list')
            rows = [row for row in rows if isinstance(row, dict) and row.get('ROUTE_ID') is not None]
            keys = Counter(schedule_key(row) for row in rows)
            self.routes = {str(row['ROUTE_ID']): row for row in rows}
            self.ambiguous = {key for key, count in keys.items() if count != 1 or not all(key)}
            self.routes_expire_at = monotonic() + CACHE_SECONDS
            return self.routes

    async def collect(self, http, details, observations, now, *, idle_routes=(), end_check_keys=()):
        to_check = {}
        for detail in details:
            for segment in detail.segments:
                if segment.type == 'bus':
                    key = (segment.routeId, segment.boardStation.stationId)
                    if key not in observations or key in end_check_keys:
                        to_check[key] = segment
        if not to_check:
            return
        try:
            async with asyncio.timeout(4):
                routes = await self.load_routes(http)
                await asyncio.gather(*(self.collect_route(http, key, segment, routes, observations, now, idle_routes)
                                       for key, segment in to_check.items()))
        except LOOKUP_ERRORS:
            pass

    async def collect_route(self, http, key, segment, routes, observations, now, idle_routes):
        route_id = segment.routeId.removeprefix('cheonan-route-')
        row = routes.get(route_id)
        if row is None or str(row.get('ROUTE_NAME') or '').strip() != segment.routeName.strip():
            return
        route_key = schedule_key(row)
        if route_key in self.ambiguous:
            return
        approach = await asyncio.to_thread(self.transport.timetable_stop_offset, route_id, segment.boardSequence,
                                           key[1].removeprefix('cheonan-stop-'))
        if approach is None:
            return
        starting_stop, travel_seconds = approach
        if str(row.get('ST_STOP_NAME') or '').split() != starting_stop.split():
            return
        name, direction, area = route_key
        params = {
            'routeName': quote(name, safe=''), 'routeDirection': direction, 'relayAreacode': area,
            'routeExplain': quote(str(row.get('ROUTE_EXPLAIN') or ''), safe=''),
            'stName': quote(str(row['ST_STOP_NAME']), safe=''),
            'edName': quote(str(row.get('ED_STOP_NAME') or ''), safe=''),
            'fstTime': row.get('FST_TIME') or '', 'lstTime': row.get('LST_TIME') or '',
            'routeDLength': row.get('ROUTE_DLENGTH') or '', 'avgTime': row.get('AVG_TIME') or '',
        }
        signature = tuple(params.items())
        try:
            async with self.slots:
                cache = self.tables.get(route_id)
                if cache and cache[0] == signature and monotonic() < cache[1]:
                    _, _, departures, fetched_at = cache
                else:
                    response = await http.get(f'{BASE}/showBusTimeTable.do', params=params)
                    response.raise_for_status()
                    departures = parse_departures(response.text)
                    if departures is None:
                        return
                    fetched_at = now
                    self.tables[route_id] = (signature, monotonic() + CACHE_SECONDS, departures, fetched_at)
        except LOOKUP_ERRORS:
            return
        expected = next_departure(departures, now, travel_seconds)
        if expected is not None:
            source = ESTIMATE_SOURCE if travel_seconds else SOURCE
            observations.setdefault(key, ArrivalObservation(expected, fetched_at=fetched_at, source_id=source))
        elif route_id in idle_routes and departures[service_day_type(now.astimezone(SEOUL).date())]:
            observations[key] = ArrivalObservation(None, operation_status='not_running', source_updated_at=now,
                                                   fetched_at=now, source_id='cheonan-bis-operation')
