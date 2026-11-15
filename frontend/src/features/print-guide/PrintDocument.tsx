import type { CSSProperties, Ref } from 'react';
import { formatBusLabel, formatDistance, formatDuration } from '../../shared/api/format';
import type { Bus, PrintData, StationRef } from '../../shared/api/types';
import { displayDetail, displayText } from '../../shared/places';
import { ORIGIN_PUBLIC_CODE, ORIGIN_STATION_ID } from '../../../config/kiosk';
import styles from './PrintDocument.module.css';

const A4_CONTENT_HEIGHT_PX = (297 - 2 * 8) * 96 / 25.4;

interface PrintDocumentProps {
  snapshot: PrintData;
  originName: string;
  destinationName: string;
  contentRef?: Ref<HTMLDivElement>;
  scale?: number;
}

export async function measurePrintScale(content: HTMLElement): Promise<number> {
  // Start font loading before measuring wrapped station names.
  content.getBoundingClientRect();
  await content.ownerDocument.fonts?.ready;
  const height = content.getBoundingClientRect().height;
  return height > A4_CONTENT_HEIGHT_PX ? A4_CONTENT_HEIGHT_PX / height : 1;
}

function paperStation(station: StationRef) {
  const code = station.arsId ?? (station.stationId === ORIGIN_STATION_ID ? ORIGIN_PUBLIC_CODE : null);
  return code ? `${displayText(station.name)} (${code})` : displayText(station.name);
}

function BoardingStop({ bus, first }: { bus: Bus; first: boolean }) {
  const direction = bus.directionLabel && displayText(bus.directionLabel);
  return (
    <div className={styles.boardingStop}>
      <p className={styles.caption}>{first ? '출발 정류장' : '환승 정류장'}</p>
      <p className={styles.boardingPlace}>
        <strong>{paperStation(bus.boardStation)}</strong>
        {direction && <span>{direction}</span>}
      </p>
    </div>
  );
}

function BusLeg({ bus, index }: { bus: Bus; index: number }) {
  const direction = bus.directionLabel && displayText(bus.directionLabel);
  const previousStop = bus.stops?.find(stop => stop.sequence === bus.alightSequence - 1);
  return (
    <li className={styles.leg}>
      <BoardingStop bus={bus} first={index === 0} />
      <section className={styles.busCard}>
        <p className={styles.caption}>{index * 2 + 1}. 이 버스를 타세요</p>
        <h2 className={styles.major}>{formatBusLabel(bus.routeName)}</h2>
        <p className={styles.ride}>{bus.stopCount}개 정류장 이동{direction && ` · ${direction}`}</p>
        {previousStop && <p>전 정류장: {paperStation(previousStop)}</p>}
      </section>
      <section className={styles.alightCard}>
        <p className={styles.caption}>{index * 2 + 2}. 여기서 내리세요</p>
        <h2 className={styles.major}>{paperStation(bus.alightStation)}</h2>
      </section>
    </li>
  );
}

export function PrintDocument({ snapshot, originName, destinationName, contentRef, scale = 1 }: PrintDocumentProps) {
  const detail = displayDetail(snapshot.routeDetail);
  const buses = detail.segments.filter(segment => segment.type === 'bus');
  const transferCount = Math.max(0, buses.length - 1);
  const lastSegment = detail.segments.at(-1);
  const finalStop = buses.at(-1)?.alightStation;
  const walk = lastSegment?.type === 'walk' ? lastSegment : null;
  const walkFrom = finalStop
    ? `${paperStation(finalStop)}${finalStop.name.endsWith('정류장') ? '' : ' 정류장'}`
    : walk?.fromLabel;
  const walkingDetails = walk ? [
    walk.distanceMeters == null ? null : `도보 ${formatDistance(walk.distanceMeters)}`,
    walk.durationSeconds == null ? null : `약 ${formatDuration(walk.durationSeconds)}`,
  ].filter(Boolean).join(' · ') : '';

  return (
    <article className={styles.document} aria-label="인쇄용 이동 안내표" data-route-version={detail.routeSnapshotVersion}>
      <div ref={contentRef} className={styles.sheetContent} style={{ '--guide-scale': scale } as CSSProperties}>
        <header className={styles.header}>
          <div className={styles.brand}><img src="/assets/31cc0.svg" alt="" />버스 길안내</div>
          <p className={styles.destinationLabel}>목적지{transferCount > 0 && ` / 환승 ${transferCount}회`}</p>
          <h1>{displayText(destinationName)}</h1>
          {detail.estimatedTravelSeconds != null && (
            <p className={styles.estimate}>예상 이동시간 {formatDuration(detail.estimatedTravelSeconds)} <small>(대기 제외)</small></p>
          )}
        </header>
        {buses.length > 0 ? (
          <ol className={styles.legs} aria-label="버스 이동 단계">
            {buses.map((bus, index) => <BusLeg key={bus.segmentId} bus={bus} index={index} />)}
          </ol>
        ) : (
          <div className={styles.boardingStop}>
            <p className={styles.caption}>출발 정류장</p>
            <p className={styles.boardingPlace}><strong>{paperStation({ stationId: detail.originStationId, name: originName })}</strong></p>
          </div>
        )}
        {walk && (
          <footer className={styles.walk}>
            <p>{walkFrom}에서 {walk.toLabel}까지 걸어가세요</p>
            {walkingDetails && <p>{walkingDetails}</p>}
          </footer>
        )}
      </div>
    </article>
  );
}
