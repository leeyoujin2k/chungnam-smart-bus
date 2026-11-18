import { describe, expect, it } from 'vitest';
import { currentArrival, effectiveFreshness, formatDateTime, formatDistance, formatDuration, formatTime } from './format';
import type { Arrival, Quality } from './types';

const now = Date.parse('2026-11-10T00:00:00Z');
const quality: Quality = { sourceId: 'fixture', sourceUpdatedAt: '2026-11-09T23:59:00Z', fetchedAt: '2026-11-10T00:00:00Z', freshness: 'fresh' };
const arrival: Arrival = {
  segmentId: 'bus', routeId: 'route', boardStationId: 'origin', arrivalStatus: 'available',
  etaSeconds: 300, expectedArrivalAt: '2026-11-10T00:05:00Z', operationStatus: 'unknown', dataQuality: quality,
};

describe('displaying verified transport facts', () => {
  it('keeps missing duration and distance distinct from actual zero', () => {
    expect(formatDuration(null)).toBe('확인되지 않음');
    expect(formatDistance(null)).toBe('확인되지 않음');
    expect(formatDuration(0)).toBe('0분');
    expect(formatDistance(0)).toBe('0m');
    expect(formatDuration(90)).toBe('2분');
  });

  it('renders time in Korea regardless of the device timezone', () => {
    expect(formatTime('2026-11-10T00:00:00Z')).toBe('09:00');
    expect(formatDateTime('2026-11-10T00:00:00Z')).toContain('2026');
    expect(formatTime(null)).toBe('확인 불가');
    expect(formatTime('invalid')).toBe('확인 불가');
  });

  it('ages source data at the 90-second boundary without promoting stale data', () => {
    expect(effectiveFreshness(quality, now + 30_000)).toBe('fresh');
    expect(effectiveFreshness(quality, now + 30_001)).toBe('stale');
    expect(effectiveFreshness({ ...quality, freshness: 'stale' }, now)).toBe('stale');
    expect(effectiveFreshness({ ...quality, sourceUpdatedAt: null }, now)).toBe('unknown');
    expect(effectiveFreshness(quality, now - 70_000)).toBe('unknown');
  });

  it('does not present unknown or old ETA as a current arrival prediction', () => {
    expect(currentArrival(arrival, now)?.label).toBe('5분후');
    expect(currentArrival(arrival, now + 31_000)).toBeNull();
    expect(currentArrival({ ...arrival, dataQuality: { ...quality, freshness: 'unknown' } }, now)).toBeNull();
    expect(currentArrival({ ...arrival, etaSeconds: null }, now)).toBeNull();
    expect(currentArrival({ ...arrival, expectedArrivalAt: quality.fetchedAt, etaSeconds: 0 }, now)?.label).toBe('0분후');
  });

  it.each(['cheonan-bis-timetable', 'cheonan-bis-timetable-estimate'])(
    'counts down from the scheduled arrival time for %s', sourceId => {
    const scheduled: Arrival = {
      ...arrival, dataQuality: { ...quality, sourceId, sourceUpdatedAt: null, freshness: 'unknown' },
    };
    expect(currentArrival(scheduled, now + 135_000)).toEqual({label: '3분후', waitSeconds: 165});
    expect(currentArrival(scheduled, now + 300_001)).toBeNull();
    expect(currentArrival({ ...scheduled, expectedArrivalAt: null }, now)).toBeNull();
  });

  it('formats upstream, recent, headway and historical estimates as minute countdowns', () => {
    const sample = (sourceId: string): Arrival => ({
      ...arrival, etaSeconds:420, expectedArrivalAt:'2026-11-10T00:06:00Z',
      dataQuality:{sourceId, sourceUpdatedAt:null, fetchedAt:'2026-11-09T23:59:00Z', freshness:'unknown'},
    });
    expect(currentArrival(sample('cheonan-bis-upstream'), now)?.label).toBe('6분후');
    const recent = sample('cheonan-bis-recent');
    expect(currentArrival(recent, now)?.label).toBe('6분후');
    expect(currentArrival(recent, now + 30_000)?.label).toBe('6분후');
    expect(currentArrival(recent, now + 30_001)).toBeNull();
    const headway = sample('cheonan-bis-headway');
    expect(currentArrival(headway, now)?.label).toBe('6분후');
    expect(currentArrival(headway, now + 360_000)).toBeNull();
    const history = {...sample('cheonan-bis-history'), expectedArrivalAt:'2026-11-10T00:10:00Z',
      expectedArrivalRangeEndAt:'2026-11-10T00:15:00Z'};
    expect(currentArrival(history, now)?.label).toBe('13분후');
    expect(currentArrival(history, now + 720_000)?.label).toBe('2분후');
    expect(currentArrival(history, now + 900_000)).toBeNull();
    expect(currentArrival({...arrival, operationStatus:'not_running'}, now)?.label).toBe('운행 종료');
  });
});
