import type { Arrival, Freshness, Quality, StationRef } from './types';

export const UNKNOWN = '확인되지 않음';
export const NO_ARRIVAL = '도착 정보 없음';

function parsed(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatTime(value: string | null | undefined): string {
  const date = parsed(value);
  return date ? new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(date) : '확인 불가';
}

export function formatDateTime(value: string | null | undefined): string {
  const date = parsed(value);
  return date ? new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(date) : '확인 불가';
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return UNKNOWN;
  return `${Math.ceil(seconds / 60)}분`;
}

export function formatDistance(meters: number | null | undefined): string {
  if (meters == null || !Number.isFinite(meters) || meters < 0) return UNKNOWN;
  return meters < 1000 ? `${Math.round(meters).toLocaleString('ko-KR')}m` : `${(meters / 1000).toLocaleString('ko-KR', { maximumFractionDigits: 1 })}km`;
}

export function formatBusLabel(routeName: string): string {
  if (routeName.endsWith('버스')) return routeName;
  return `${routeName}${/\d$/.test(routeName) ? '번' : ''} 버스`;
}

export function freshnessLabel(freshness: Freshness): string {
  return { fresh: '최근 정보', stale: '오래된 정보', unknown: '갱신 상태 확인 불가' }[freshness];
}

/** Source timestamps do not become fresh just because this browser fetched again. */
export function effectiveFreshness(quality: Quality, now = Date.now()): Freshness {
  if (quality.freshness !== 'fresh') return quality.freshness;
  const source = parsed(quality.sourceUpdatedAt);
  if (!source || source.getTime() > now) return 'unknown';
  return now - source.getTime() > 90_000 ? 'stale' : 'fresh';
}

export function currentArrival(arrival: Arrival | undefined, now = Date.now()) {
  if (!arrival) return null;
  const freshness = effectiveFreshness(arrival.dataQuality, now);
  const source = arrival.dataQuality.sourceId;
  if (arrival.operationStatus === 'not_running') {
    return freshness === 'fresh' ? {label:'운행 종료', waitSeconds:null} : null;
  }
  if (arrival.arrivalStatus !== 'available' || arrival.etaSeconds == null) return null;
  const expected = parsed(arrival.expectedArrivalAt);
  const seconds = expected ? (expected.getTime() - now) / 1000 : arrival.etaSeconds;
  if (!Number.isFinite(seconds)) return null;
  const timestamp = parsed(arrival.dataQuality.sourceUpdatedAt) ?? parsed(arrival.dataQuality.fetchedAt);
  const age = timestamp ? now - timestamp.getTime() : Infinity;
  const recent = age >= 0 && age <= 90_000 && freshness !== 'stale';
  const waitSeconds = Math.max(0, seconds);
  const countdown = (displaySeconds = waitSeconds) => ({label:`${formatDuration(displaySeconds)}후`, waitSeconds});

  if (source === 'cheonan-bis-upstream' || source === 'cheonan-bis-recent') {
    return expected && recent && seconds > 0 ? countdown() : null;
  }
  if (source === 'cheonan-bis-timetable' || source === 'cheonan-bis-timetable-estimate') {
    return expected && seconds >= 0 ? countdown() : null;
  }
  if (source === 'cheonan-bis-headway') {
    return expected && seconds > 0 ? countdown() : null;
  }
  if (source === 'cheonan-bis-history') {
    const last = parsed(arrival.expectedArrivalRangeEndAt);
    if (!expected || !last || last.getTime() <= now || last < expected) return null;
    const latestSeconds = (last.getTime() - now) / 1000;
    return countdown((waitSeconds + latestSeconds) / 2);
  }

  const recentQuery = source === 'cheonan-bis-arrival' && recent;
  if (freshness !== 'fresh' && !recentQuery) return null;
  if (seconds < 0 && arrival.etaSeconds !== 0) return null;
  return countdown();
}
export function formatStation(station: StationRef): string {
  return station.arsId ? `${station.name} (${station.arsId}번)` : station.name;
}
