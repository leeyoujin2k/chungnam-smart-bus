import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ORIGIN_STATION_ID } from '../../../config/kiosk';
import type { PrintData, Quality } from '../../shared/api/types';
import { PrintDocument } from './PrintDocument';

const generatedAt = '2026-11-10T04:00:00Z';
const unknown: Quality = { sourceId: null, sourceUpdatedAt: null, fetchedAt: null, freshness: 'unknown' };

function snapshot(): PrintData {
  return {
    printGuideId: 'print-test', generatedAt,
    notice: '시연 데이터 · 실제 운행 정보 아님',
    routeDetail: {
      routeSearchId: 'search-test', routeOptionId: 'option-test', routeSnapshotVersion: 'v1',
      originStationId: 'start', destinationId: 'destination', computedAt: generatedAt,
      journeyStatus: 'ready', referenceDataQuality: unknown,
      segments: [
        { type: 'bus', segmentId: 'bus-1', routeId: 'route-1', routeName: '시연 1번',
          boardStation: { stationId: 'start', name: '시연 출발 정류장' },
          alightStation: { stationId: 'transfer', name: '시연 환승 정류장' },
          boardSequence: 1, alightSequence: 3, stopCount: 2, durationSeconds: null },
        { type: 'transfer', segmentId: 'transfer-1', fromSegmentId: 'bus-1', toSegmentId: 'bus-2',
          atStation: { stationId: 'transfer', name: '시연 환승 정류장' }, instructionTemplateId: 'transfer.basic' },
        { type: 'bus', segmentId: 'bus-2', routeId: 'route-2', routeName: '시연 2',
          boardStation: { stationId: 'transfer', name: '시연 환승 정류장' },
          alightStation: { stationId: 'end', name: '시연 도착 정류장' },
          boardSequence: 1, alightSequence: 2, stopCount: 1, durationSeconds: null },
        { type: 'walk', segmentId: 'walk-1', fromLabel: '시연 도착 정류장', toLabel: '시연 터미널',
          distanceMeters: null, durationSeconds: null },
      ],
    },
    liveInfoSnapshot: {
      routeSnapshotVersion: 'v1',
      arrivals: [{ segmentId: 'bus-1', routeId: 'route-1', boardStationId: 'start',
        arrivalStatus: 'available', etaSeconds: 987655, expectedArrivalAt: '2026-11-10T17:42:00Z',
        operationStatus: 'unknown', dataQuality: unknown }],
      serviceChanges: { coverageStatus: 'unknown', items: [], dataQuality: unknown },
    },
    guidance: { routeSnapshotVersion: 'v1', mode: 'template_fallback', steps: [] },
  };
}

function render(data: PrintData): HTMLElement {
  const host = document.createElement('div');
  host.innerHTML = renderToStaticMarkup(<PrintDocument snapshot={data} originName="시연 출발 정류장" destinationName="시연 터미널" />);
  return host;
}

describe('고정 인쇄 안내표', () => {
  it('환승 시 다음 버스와 타는 곳을 바로 출력하고 변동하는 도착 예측은 출력하지 않는다', () => {
    const doc = render(snapshot());
    const steps = Array.from(doc.querySelectorAll('ol > li'));
    expect(doc.querySelector('header')?.textContent).toContain('목적지 / 환승 1회');
    expect(steps).toHaveLength(2);
    expect(steps.map(step => Array.from(step.querySelectorAll('h2'), title => title.textContent))).toEqual([
      ['1번 버스', '환승 정류장'], ['2번 버스', '도착 정류장'],
    ]);
    expect(steps[0].textContent).toMatch(/1\. 이 버스를 타세요.*2\. 여기서 내리세요/);
    expect(steps[1].textContent).toMatch(/환승 정류장.*3\. 이 버스를 타세요.*4\. 여기서 내리세요/);
    expect(steps[0].textContent).toContain('2개 정류장');
    expect(doc.querySelector('footer')?.textContent).toContain('도착 정류장에서 터미널까지 걸어가세요');
    expect(doc.querySelector('footer')?.textContent).not.toMatch(/확인되지 않음|0m|0분/);
    expect(doc.textContent).not.toContain('전 정류장:');
    expect(doc.textContent).not.toMatch(/시연|테스트용|가상 시설|더미 데이터/);
    expect(doc.textContent).not.toMatch(/운행 상태|운행 변경 정보 확인|원천 갱신|서버 수집|카운트다운|자료 출처/);
    expect(doc.textContent).not.toContain('987655');
    expect(doc.textContent).not.toContain('02:42');
    expect(doc.textContent).not.toContain('0분');
    expect(doc.querySelectorAll('h1')).toHaveLength(1);
  });

  it('현재 경로의 안내 문장이 있어도 같은 이동 지시를 반복 인쇄하지 않는다', () => {
    const data = snapshot();
    const text = '출발 정류장에서 1번 버스를 타세요. 환승 정류장에서 내리세요.';
    data.guidance = { routeSnapshotVersion: 'v1', mode: 'template_fallback', steps: [{ segmentId: 'bus-1', text }] };
    const doc = render(data);
    expect(doc.textContent).not.toContain(text);
    expect(doc.querySelectorAll('ol > li')).toHaveLength(2);
    expect(doc.querySelector('article')?.getAttribute('data-route-version')).toBe('v1');
  });
});


it('연결 전 운행 상태를 실시간으로 출력하거나 반복 시연 문구를 노출하지 않는다', () => {
  const data=snapshot(); data.liveInfoSnapshot.arrivals[0].operationStatus='running';
  const html=renderToStaticMarkup(<PrintDocument snapshot={data} originName="시연 출발 정류장" destinationName="시연 병원" />);
  const host=document.createElement('div'); host.innerHTML=html;
  expect(host.textContent).not.toMatch(/시연|테스트용|가상 시설|더미 데이터|운행 중으로 제공됨/);
  expect(host.textContent).not.toMatch(/실시간 운행 정보가 아직|운행 상태|원천 갱신|서버 수집|카운트다운/);
  expect(data.routeDetail.segments[0].type==='bus' && data.routeDetail.segments[0].routeName).toBe('시연 1번');
});


it('직행 양식에서 실제 하차 직전 정류장과 하차 후 도보 정보를 출력한다', () => {
  const data = snapshot();
  data.routeDetail.originStationId = ORIGIN_STATION_ID;
  const firstBus = data.routeDetail.segments[0];
  if (firstBus.type !== 'bus') throw new Error('첫 구간에 버스가 없습니다.');
  firstBus.boardStation = { stationId: ORIGIN_STATION_ID, name: '종합터미널' };
  firstBus.alightStation = { stationId: 'end', name: '병원입구', arsId: '1035' };
  firstBus.directionLabel = '두정역 방면';
  const coordinates = { latitude: 36.819, longitude: 127.156 };
  firstBus.stops = [
    { ...firstBus.boardStation, sequence: 1, coordinates },
    { stationId: 'previous', name: '시연 삼거리', arsId: '1034', sequence: 2, coordinates },
    { ...firstBus.alightStation, sequence: 3, coordinates },
  ];
  data.routeDetail.estimatedTravelSeconds = 18 * 60;
  data.routeDetail.routingNotice = '공식 노선 구조 기준 · 평균 이동시간 추정 · 대기시간 제외 · OpenStreetMap contributors';
  const walk = data.routeDetail.segments.at(-1);
  if (walk?.type !== 'walk') throw new Error('마지막 구간에 도보가 없습니다.');
  Object.assign(walk, { fromLabel: '병원입구 정류장', toLabel: '병원', distanceMeters: 170, durationSeconds: 180 });
  data.routeDetail.segments = [firstBus, walk];
  const before = JSON.stringify(data);
  const host = document.createElement('div');
  host.innerHTML = renderToStaticMarkup(<PrintDocument snapshot={data} originName="종합터미널" destinationName="병원" />);
  expect(host.querySelector('h1')?.textContent).toBe('병원');
  expect(host.textContent).toContain('예상 이동시간 18분 (대기 제외)');
  expect(host.textContent).toContain('출발 정류장종합터미널 (686)두정역 방면');
  expect(host.textContent).toContain('전 정류장: 삼거리 (1034)');
  expect(host.textContent).toContain('병원입구 (1035) 정류장에서 병원까지 걸어가세요');
  expect(host.textContent).toContain('도보 170m · 약 3분');
  expect(host.querySelectorAll('ol > li')).toHaveLength(1);
  expect(host.textContent).not.toMatch(/환승|3\. 이 버스를 타세요/);
  expect(host.textContent).not.toMatch(/공식 노선|대기시간 제외|OpenStreetMap|원천|서버 수집|카운트다운|확인 불가|출발 정류장은 제외/);
  expect(JSON.stringify(data)).toBe(before);
});
