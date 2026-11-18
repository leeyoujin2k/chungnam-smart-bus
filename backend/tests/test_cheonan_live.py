"""Municipal responses can be partly malformed without hiding valid arrivals."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.database import Database
from backend.integrations.cheonan_history import Boarding, CheonanArrivalHistory
from backend.integrations.cheonan_live import HEADWAY, HISTORY, LIVE, RECENT, UPSTREAM, arrival_state
from backend.integrations.cheonan_timetables import CheonanTimetables, next_departure, parse_departures
from backend.models import schemas as s
from backend.services.cheonan_routing import CheonanNetwork
from backend.services.journeys import arrival_values


def detail():
    now = datetime.now(timezone.utc)
    buses = [s.Bus(segmentId=f'bus-{i}', routeId=f'cheonan-route-{rid}', routeName=str(i),
        boardStation=s.StationRef(stationId=f'cheonan-stop-{sid}', name='승차 정류장'),
        alightStation=s.StationRef(stationId='cheonan-stop-end', name='하차 정류장'),
        boardSequence=1, alightSequence=2, stopCount=1, durationSeconds=None)
        for i, (rid, sid) in enumerate((('285000007', '285000686'), ('285000008', '285000687')), 1)]
    return now, s.RouteDetail(routeSearchId='search', routeOptionId='option', routeSnapshotVersion='v',
        originStationId='cheonan-stop-285000686', destinationId='destination', computedAt=now,
        journeyStatus='ready', referenceDataQuality=s.Quality(sourceId=None, sourceUpdatedAt=None,
            fetchedAt=None, freshness='unknown'), segments=buses)


@pytest.mark.anyio
async def test_one_bad_row_or_station_does_not_hide_other_valid_observations():
    now, selected = detail()
    def handler(request):
        if request.url.params['stopId'] == '285000687':
            return httpx.Response(503)
        return httpx.Response(200, json={'resultList': [None, 'bad row', {},
            {'ROUTE_ID': 285000007, 'RTIME': True}, {'ROUTE_ID': 285000007, 'RTIME': '1'},
            {'ROUTE_ID': 285000007, 'RTIME': -1}, {'ROUTE_ID': 285000007, 'RTIME': 181},
            {'ROUTE_ID': 285000007, 'RTIME': 7}, {'ROUTE_ID': 285000007, 'RTIME': 3},
            {'ROUTE_ID': 999, 'RTIME': 0}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        state = await arrival_state(selected, now, client=client)
    assert len(state.arrivals) == 1
    observed = state.arrivals[('cheonan-route-285000007', 'cheonan-stop-285000686')]
    assert observed.expected_arrival_at == now + timedelta(minutes=3)
    assert observed.source_updated_at is None
    assert observed.operation_status == 'unknown'


@pytest.mark.anyio
@pytest.mark.parametrize('payload', [None, [], {}, {'resultList': None}, {'resultList': {}}, {'resultList': []}])
async def test_missing_or_invalid_payload_remains_unavailable(payload):
    now, selected = detail()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        state = await arrival_state(selected, now, client=client)
    assert not state.arrivals_available
    assert state.arrivals == {}


TIMETABLE_HTML = '''
<dl><dt>평일 출발시간</dt><dd>17:20</dd><dd>23:55</dd><dd>00:15</dd></dl>
<dl><dt>주말 출발시간</dt><dd>17:25</dd></dl>
<dl><dt>공휴일 출발시간</dt><dd>17:30</dd></dl>
<dl><dt>임시 출발시간</dt><dd>17:10</dd></dl>
'''


@pytest.mark.parametrize(('local', 'expected'), [
    ('2026-11-18T17:10:00+09:00', '2026-11-18T17:20:00+09:00'),
    ('2026-11-21T17:10:00+09:00', '2026-11-21T17:25:00+09:00'),
    ('2026-11-22T17:10:00+09:00', '2026-11-22T17:30:00+09:00'),
    ('2026-12-25T17:10:00+09:00', '2026-12-25T17:30:00+09:00'),
    ('2026-11-19T00:10:00+09:00', '2026-11-19T00:15:00+09:00'),
    ('2026-11-21T17:26:00+09:00', None),
])
def test_timetable_uses_korean_service_day_and_skips_elapsed_departures(local, expected):
    departures = parse_departures(TIMETABLE_HTML)
    now = datetime.fromisoformat(local).astimezone(timezone.utc)
    assert next_departure(departures, now) == (datetime.fromisoformat(expected) if expected else None)


def test_estimated_arrival_after_midnight_uses_the_departure_days_timetable():
    departures = parse_departures(TIMETABLE_HTML)
    now = datetime.fromisoformat('2026-11-21T00:02:00+09:00')
    assert next_departure(departures, now, travel_seconds=600) == datetime.fromisoformat('2026-11-21T00:05:00+09:00')


@pytest.mark.anyio
async def test_timetable_fills_only_missing_arrivals_at_the_kiosk():
    _, selected = detail()
    now = datetime(2026, 11, 18, 8, 10, 21, tzinfo=timezone.utc)
    network = CheonanNetwork()
    route_id = '285000003'
    boarding = network.routes[route_id]['stops'].index(network.origin['publicId'])
    approach_seconds = network.bus(route_id, 0, boarding, 'approach').durationSeconds
    assert approach_seconds is not None and approach_seconds > 60
    passing_bus = network.bus(route_id, boarding, boarding + 1, 'bus-2')
    invalid_stop = passing_bus.model_copy(update={'boardStation': network.ref('285000685')})
    invalid_sequence = passing_bus.model_copy(update={'boardSequence': 2})
    selected = selected.model_copy(update={'segments': [
        network.bus('285000032', 0, 1, 'bus-51'), network.bus('285000034', 0, 1, 'bus-52'),
        passing_bus, invalid_stop,
    ]})
    timetable_requests = []

    def handler(request):
        if request.url.path.endswith('/getArrivalScheduleList.do'):
            return httpx.Response(200, json={'resultList': [{'ROUTE_ID': 285000034, 'RTIME': 3}]})
        if request.url.path.endswith('/getRouteList.do'):
            return httpx.Response(200, json={'resultList': [
                {'ROUTE_ID': 285000032, 'ROUTE_NAME': '51', 'ROUTE_DIRECTION': 1, 'RELAY_AREACODE': 285,
                 'ST_STOP_NAME': '종합터미널'},
                {'ROUTE_ID': 285000033, 'ROUTE_NAME': '51', 'ROUTE_DIRECTION': 2, 'RELAY_AREACODE': 285,
                 'ST_STOP_NAME': '각원사회차지'},
                {'ROUTE_ID': 285000003, 'ROUTE_NAME': '2', 'ROUTE_DIRECTION': 1, 'RELAY_AREACODE': 285,
                 'ST_STOP_NAME': '두정역입구'},
            ]})
        assert request.url.path.endswith('/showBusTimeTable.do')
        timetable_requests.append((request.url.params['routeName'], request.url.params['routeDirection']))
        return httpx.Response(200, text=TIMETABLE_HTML)

    timetables = CheonanTimetables(network)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        state = await arrival_state(selected, now, client=client, timetables=timetables)
        later = now + timedelta(minutes=1)
        state_later = await arrival_state(selected, later, client=client, timetables=timetables)
        after_departure = datetime.fromisoformat('2026-11-18T17:21:00+09:00')
        state_after = await arrival_state(selected, after_departure, client=client, timetables=timetables)
        invalid = await arrival_state(selected.model_copy(update={'segments': [invalid_sequence]}), now,
                                      client=client, timetables=timetables)

    values = arrival_values(selected, state, now)
    assert [(a.etaSeconds, a.dataQuality.sourceId) for a in values] == [
        (579, 'cheonan-bis-timetable'), (180, 'cheonan-bis-arrival'),
        (579 + approach_seconds, 'cheonan-bis-timetable-estimate'), (None, None),
    ]
    assert arrival_values(selected, state_later, later)[0].etaSeconds == 519
    assert arrival_values(selected, state_after, after_departure)[2].etaSeconds == approach_seconds - 60
    assert invalid.arrivals == {}
    assert sorted(timetable_requests) == [('2', '1'), ('51', '1')]


@pytest.mark.anyio
async def test_live_and_all_five_fallbacks_in_priority_order():
    network = CheonanNetwork()
    raw_route_id = '285000003'
    stops = network.routes[raw_route_id]['stops']
    board_index = stops.index(network.origin['publicId'])
    bus = network.bus(raw_route_id, board_index, board_index + 1, 'bus-2')
    boarding = Boarding(bus.routeId, bus.boardStation.stationId, '1', bus.boardSequence)
    _, selected = detail()
    selected = selected.model_copy(update={'segments':[bus]})
    now = datetime.fromisoformat('2026-11-18T15:20:00+09:00')
    db = Database('sqlite:///:memory:')
    db.initialize()
    history = CheonanArrivalHistory(db, network)
    timetables = CheonanTimetables(network)

    def passage(day, clock, direction='1', station=boarding.station_id):
        return {'route_id':bus.routeId, 'direction':direction, 'station_id':station, 'sequence':bus.boardSequence,
                'vehicle_id':'12345', 'passed_at':datetime.fromisoformat(f'2026-11-{day}T{clock}+09:00').timestamp()}

    history.record([passage('18', '15:10:00'), passage('18', '15:19:00', direction='2'),
                    passage('18', '15:19:00', station='cheonan-stop-285000685'), *[
                        passage(day, clock) for day, following in [('13','15:36:00'),('16','15:38:00'),('17','15:41:00')]
                        for clock in ('15:15:00', following)
                    ]], now)
    good = {'ROUTE_ID':int(raw_route_id), 'ROUTE_DIRECTION':'1', 'VEH_ID':12345, 'ROUTESECT_GROUP_ID':'g', 'RTIME':7}
    direct = [good, {**good,'ROUTE_DIRECTION':'2','RTIME':1},
              {**good,'VEH_ID':56565,'RTIME':0,'OPERATION_STATUS':'결행'}]
    upstream = []
    upstream_index = board_index - 1
    vehicle_index = board_index - 4
    schedule_available = True
    timetable_html = TIMETABLE_HTML.replace('17:20', '15:40')

    def handler(request):
        path = request.url.path
        if path.endswith('/getArrivalScheduleList.do'):
            stop = request.url.params['stopId']
            rows = direct if stop == stops[board_index] else upstream if stop == stops[upstream_index] else []
            return httpx.Response(200, json={'resultList':rows})
        if path.endswith('/getRouteList.do'):
            return httpx.Response(200, json={'resultList':[{'ROUTE_ID':int(raw_route_id), 'ROUTE_NAME':'2',
                'ROUTE_DIRECTION':'1', 'ST_STOP_NAME':'두정역입구', 'RELAY_AREACODE':285, 'ALLOC_TIME':'15분'}]})
        if path.endswith('/getRealtimeBusLocPopList.do'):
            return httpx.Response(200, json={'resultList':[
                {'ROUTE_ID':int(raw_route_id), 'STOP_ORD':index+1, 'STOP_ID':sid,
                 'VEH_ID':'12345' if index == vehicle_index else '9', 'ROUTESECT_GROUP_ID':'g'}
                for index, sid in enumerate(stops)
            ]})
        assert path.endswith('/showBusTimeTable.do')
        return httpx.Response(200, text=timetable_html) if schedule_available else httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async def query(at):
            state = await arrival_state(selected, at, client=client, history=history, timetables=timetables)
            return arrival_values(selected, state, at)[0]

        first = await query(now - timedelta(seconds=60))
        assert (first.etaSeconds, first.dataQuality.sourceId) == (420, LIVE)
        direct.clear()
        upstream[:] = [{**good, 'RTIME':2}, {**good, 'ROUTE_DIRECTION':'2','RTIME':0},
                       {**good, 'VEH_ID':99999,'RTIME':0}]
        estimate = await query(now)
        travel = network.bus(raw_route_id, board_index-1, board_index, 'upstream-estimate').durationSeconds
        assert (estimate.etaSeconds, estimate.dataQuality.sourceId) == (120 + travel, UPSTREAM)
        upstream_index = vehicle_index + 1
        farther = await query(now)
        travel = network.bus(raw_route_id, upstream_index, board_index, 'upstream-estimate').durationSeconds
        assert (farther.etaSeconds, farther.dataQuality.sourceId) == (120 + travel, UPSTREAM)
        upstream.clear()
        recent = await query(now)
        assert (recent.etaSeconds, recent.dataQuality.sourceId) == (360, RECENT)
        assert recent.dataQuality.fetchedAt == now - timedelta(seconds=60)
        assert (await query(now + timedelta(seconds=30))).dataQuality.sourceId == RECENT
        timetable = await query(now + timedelta(seconds=31))
        assert timetable.dataQuality.sourceId == 'cheonan-bis-timetable-estimate'
        assert timetable.expectedArrivalAt > now + timedelta(minutes=20)
        schedule_available = False
        timetables.tables.clear()
        headway = await query(now + timedelta(minutes=1))
        assert (headway.etaSeconds, headway.dataQuality.sourceId) == (240, HEADWAY)
        historical = await query(now + timedelta(minutes=6))
        assert (historical.etaSeconds, historical.dataQuality.sourceId) == (600, HISTORY)
        assert historical.expectedArrivalRangeEndAt == now + timedelta(minutes=21)
        absent = await query(now + timedelta(hours=5))
        assert (absent.arrivalStatus, absent.expectedArrivalAt) == ('unavailable', None)
        direct.append({**good,'RTIME':2})
        restored = await query(now + timedelta(hours=5, seconds=1))
        assert (restored.etaSeconds, restored.dataQuality.sourceId) == (120, LIVE)
        direct.append({'ROUTE_ID':int(raw_route_id), 'ROUTE_DIRECTION':'1','OPERATION_STATUS':'운행 종료'})
        ended = await query(now + timedelta(hours=5, seconds=2))
        assert (ended.operationStatus, ended.expectedArrivalAt) == ('not_running', None)

        # A verified end of service also overrides a still-fresh cached ETA.
        end_time = now + timedelta(hours=5, minutes=1)
        direct[:] = [good]
        assert (await query(end_time)).dataQuality.sourceId == LIVE
        direct.clear()
        schedule_available = True
        timetable_html = timetable_html.replace('<dd>23:55</dd><dd>00:15</dd>', '')
        vehicle_index = None
        ended = await query(end_time + timedelta(seconds=30))
        assert (ended.operationStatus, ended.expectedArrivalAt) == ('not_running', None)
        assert boarding not in history.recent
        direct[:] = [good]
        assert (await query(end_time + timedelta(seconds=31))).dataQuality.sourceId == LIVE

    db.engine.dispose()


@pytest.mark.anyio
async def test_position_samples_store_only_confirmed_crossings():
    network = CheonanNetwork()
    raw_route_id = '285000003'
    stops = network.routes[raw_route_id]['stops']
    board_sequence = stops.index(network.origin['publicId']) + 1
    boarding = Boarding('cheonan-route-' + raw_route_id, network.origin['stationId'], '1', board_sequence)
    db = Database('sqlite:///:memory:')
    db.initialize()
    history = CheonanArrivalHistory(db, network)
    now = datetime.fromisoformat('2026-11-18T15:20:00+09:00')
    current_sequence = board_sequence
    cancelled = False

    def handler(request):
        return httpx.Response(200, json={'resultList':[
            {'ROUTE_ID':raw_route_id, 'STOP_ORD':index+1, 'STOP_ID':sid, 'ROUTESECT_GROUP_ID':'g',
             'VEH_ID':'12345' if index+1 == current_sequence else '9',
             'OPERATION_STATUS':'결행' if cancelled and index+1 == current_sequence else None}
            for index, sid in enumerate(stops)
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await history.sample(client, '1', [boarding], now)
        current_sequence += 1
        passed = now + timedelta(seconds=30)
        await history.sample(client, '1', [boarding], passed)
        assert history.passages([boarding], passed)[boarding] == [passed]
        current_sequence = board_sequence
        await history.sample(client, '1', [boarding], now + timedelta(seconds=60))
        current_sequence += 1
        cancelled = True
        later = now + timedelta(seconds=90)
        await history.sample(client, '1', [boarding], later)
        assert history.passages([boarding], later)[boarding] == [passed]
    db.engine.dispose()
