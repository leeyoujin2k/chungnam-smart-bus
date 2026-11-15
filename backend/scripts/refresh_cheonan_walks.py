"""One-time OSM/Valhalla pedestrian matrix import; cached, one request/second.

Only road-routed links are approved. A straight-line proximity test is a bounded
candidate lookup, not evidence of pedestrian access. No automotive routes used.
"""
import json
from itertools import batched
from hashlib import sha256
from math import isfinite
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.services.geo import haversine_meters
from backend.services.cheonan_routing import MAX_ALIGHT_WALK_METERS

URL = 'https://valhalla1.openstreetmap.de/sources_to_targets'
CACHE = ROOT / 'backend/.local/cheonan-walk-import'
MATRIX_BATCH_SIZE = 32


def point(coordinates):
    return {'lat': coordinates['latitude'], 'lon': coordinates['longitude']}


def matrix(client, key, sources, targets, *, shortest=False):
    path = CACHE / (key + '.json')
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    pedestrian = {'walking_speed': 4.0}
    if shortest:
        pedestrian['shortest'] = True
    response = client.get(URL, params={'json': json.dumps({'sources': sources, 'targets': targets,
        'costing': 'pedestrian', 'units': 'kilometers', 'costing_options': {'pedestrian': pedestrian}})})
    response.raise_for_status()
    data = response.json()
    path.write_text(json.dumps(data), encoding='utf-8')
    time.sleep(1.1)
    return data


def alighting_matrix(client, destination_id, sources, targets, *, shortest=False):
    coordinates = json.dumps([sources, targets], separators=(',', ':'))
    revision = sha256(coordinates.encode()).hexdigest()[:16]
    mode = 'alight-shortest' if shortest else 'alight'
    return matrix(client, f'{destination_id}-{mode}-{revision}', sources, targets, shortest=shortest)


def link(value):
    distance, duration = value.get('distance'), value.get('time')
    if any(type(v) not in (int, float) or not isfinite(v) or v < 0 for v in (distance, duration)):
        return None
    return {'distanceMeters': round(distance * 1000), 'durationSeconds': round(duration)}


def complete_direct_walks():
    """Refresh origin-to-place paths without changing the approved alighting access."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = ROOT / 'shared/data/cheonan-walks.json'
    output = json.loads(path.read_text(encoding='utf-8'))
    transit = json.loads((ROOT / 'shared/data/cheonan-transport.json').read_text(encoding='utf-8'))
    places = json.loads((ROOT / 'shared/data/cheonan-places.json').read_text(encoding='utf-8'))['places']
    origin = transit['origin']
    if output.get('originStationId') != origin['stationId']:
        output.pop('originAccess', None)
        for entry in output['destinations'].values():
            entry.pop('directWalk', None)
            entry.pop('directWalkRetrievedAt', None)
    output['originStationId'] = origin['stationId']
    with httpx.Client(timeout=40, headers={'User-Agent': 'CheonanSmartBus/0.1 local-reference-import'}) as client:
        for place in places:
            entry = output['destinations'].get(place['destinationId'])
            if entry is None or entry.get('directWalk'):
                continue
            try:
                data = matrix(client, place['destinationId'] + '-direct-' + origin['publicId'],
                    [point(origin['coordinates'])], [point(place['coordinates'])])
                walk = link(data['sources_to_targets'][0][0])
            except (httpx.HTTPError, KeyError, ValueError, IndexError) as exc:
                print(place['destinationId'], type(exc).__name__, flush=True)
                continue
            if not walk:
                continue
            entry['directWalk'] = walk
            entry['directWalkRetrievedAt'] = datetime.now(timezone.utc).isoformat()
            print(place['destinationId'], walk, flush=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    transit = json.loads((ROOT / 'shared/data/cheonan-transport.json').read_text(encoding='utf-8'))
    places = json.loads((ROOT / 'shared/data/cheonan-places.json').read_text(encoding='utf-8'))['places']
    origin = transit['origin']['coordinates']
    used = {sid for route in transit['routes'] for sid in route['stops']}
    stations = {sid: {'latitude': row['latitude'], 'longitude': row['longitude']} for sid, row in transit['stations'].items() if sid in used}
    output = {'schemaVersion': 1, 'sourceUrl': 'https://valhalla.openstreetmap.de/',
              'attribution': '© OpenStreetMap contributors (ODbL) · Valhalla / FOSSGIS',
              'costing': 'pedestrian', 'walkingSpeedKmh': 4.0,
              'retrievedAt': datetime.now(timezone.utc).isoformat(), 'originStationId': transit['origin']['stationId'],
              'destinations': {}, 'failures': []}
    with httpx.Client(timeout=40, headers={'User-Agent': 'CheonanSmartBus/0.1 local-reference-import'}) as client:
        for place in places:
            coordinates = place.get('coordinates')
            if not coordinates:
                output['failures'].append(place['destinationId'])
                continue
            nearby = sorted(sid for sid, pos in stations.items()
                            if haversine_meters(coordinates, pos) <= MAX_ALIGHT_WALK_METERS)
            target = [point(coordinates)]
            try:
                excluded = []
                walks = []
                for batch in batched(nearby, MATRIX_BATCH_SIZE):
                    data = alighting_matrix(client, place['destinationId'],
                                            [point(stations[sid]) for sid in batch], target)
                    for sid, row in zip(batch, data['sources_to_targets'], strict=True):
                        walk = link(row[0])
                        if walk and walk['distanceMeters'] <= MAX_ALIGHT_WALK_METERS:
                            walks.append({'stationId': sid, **walk})
                        else:
                            excluded.append(sid)
                # The usual pedestrian route can prefer paths and crossings
                # that make it longer than the shortest available walking route.
                for batch in batched(excluded, MATRIX_BATCH_SIZE):
                    data = alighting_matrix(client, place['destinationId'],
                                            [point(stations[sid]) for sid in batch], target, shortest=True)
                    for sid, row in zip(batch, data['sources_to_targets'], strict=True):
                        walk = link(row[0])
                        if walk and walk['distanceMeters'] <= MAX_ALIGHT_WALK_METERS:
                            walks.append({'stationId': sid, **walk, 'pedestrianRouting': 'shortest'})
                data = matrix(client, place['destinationId'] + '-direct-' + transit['origin']['publicId'],
                              [point(origin)], target)
                output['destinations'][place['destinationId']] = {'access': sorted(walks, key=lambda walk: walk['stationId']),
                    'directWalk': link(data['sources_to_targets'][0][0])}
                print(place['destinationId'], len(walks), flush=True)
            except (httpx.HTTPError, KeyError, ValueError, IndexError):
                output['failures'].append(place['destinationId'])
        if output['failures']:
            raise RuntimeError(f"Walking reference incomplete; existing snapshot preserved: {output['failures']}")
        (ROOT / 'shared/data/cheonan-walks.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('Walking reference saved; failures:', output['failures'], flush=True)


if __name__ == '__main__':
    complete_direct_walks() if '--complete-direct' in sys.argv else main()
