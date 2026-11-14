import { Fragment, useState, type ReactNode } from 'react';
import type { OptionArrivals, RouteOption, StationRef } from '../../shared/api/types';
import { currentArrival, formatBusLabel, formatDistance, formatDuration } from '../../shared/api/format';
import { routeTiming, simulatedArrivalLabel } from '../../shared/prototypeTiming';
import { displayText } from '../../shared/places';
import s from '../../app/App.module.css';

interface Props {
  options: RouteOption[];
  recommended?: boolean;
  selected: string | null;
  busy: boolean;
  simulate: boolean;
  sortByWait: boolean;
  checkingArrivals: boolean;
  now: number;
  arrivals?: Record<string, OptionArrivals>;
  onChoose: (option: RouteOption) => void;
}

function Icon({file, className = ''}: {file: string; className?: string}) {
  return <span className={`${s.asset} ${s.cardIcon} ${className}`} aria-hidden="true"><img src={`/assets/${file}`} alt="" /></span>;
}

export function RouteCardHeader({names, transferCount, duration, arrival, action, busChoices}: {
  names: string[]; transferCount: number; duration: number | null; arrival?: string; action: ReactNode; busChoices?: ReactNode;
}) {
  return <span className={s.routeCardHeader}>
    <span className={s.routeCardInfo}>
      <span className={`${s.routeLine} ${busChoices ? s.routeLineWithChoices : ''}`}>
        <span className={s.routeKind}>{names.length ? (transferCount ? `환승 ${transferCount}회` : '직행') : '도보'}</span>
        {busChoices ?? names.map((name, index) => <Fragment key={index}>
          {index > 0 && <Icon file="3aff1.svg" className={s.routeBusArrow} />}
          <strong>{formatBusLabel(name).replace(/ 버스$/, '')}</strong>
        </Fragment>)}
      </span>
      <span className={s.routeTimes}>
        <strong aria-label={`예상 이동시간 ${formatDuration(duration)}`}>{duration == null ? '이동시간 확인 불가' : `약 ${formatDuration(duration)}`}</strong>
        {arrival && <span>{arrival}</span>}
      </span>
    </span>
    {action}
  </span>;
}

function stationLabel(station: StationRef) {
  return station.arsId ? `${station.name} ${station.arsId}` : station.name;
}

interface RouteCardData {
  option: RouteOption;
  arrival: string;
  wait: number;
}

function RouteCardStations({option}: {option: RouteOption}) {
  if (!option.boardStation || !option.alightStation) {
    return <span className={s.routeStationText}>{option.routeNames.length
      ? '정류장은 상세 안내에서 확인해 주세요.' : '현재 정류장에서 목적지까지 도보 이동'}</span>;
  }
  const stops = [option.boardStation, ...(option.transferStations ?? []), option.alightStation];
  return <span className={`${s.routeStations} ${stops.length > 2 ? s.routeTransferStations : ''}`}>
    <Icon file={stops.length > 2 ? '51494.svg' : '26246.svg'} className={s.routeStationLine} />
    {stops.map((stop, index) => {
      const last = index === stops.length - 1;
      const direction = last ? null : option.directionLabels?.[index];
      return <span className={s.routeStation} key={`${stop.stationId}-${index}`}>
        <Icon file={index === 0 ? '92c82.svg' : 'a8aeb.svg'} />
        <span className={s.routeStationDetails}>
          <span>{stationLabel(stop)}</span>
          {direction && <small>{index > 0 ? '환승 · ' : ''}{direction}</small>}
        </span>
      </span>;
    })}
  </span>;
}

function RouteNotes({option}: {option: RouteOption}) {
  return <>
    {option.routeNames.length > 0 && option.walkingDistanceMeters != null &&
      <span className={s.routeStationText}>하차 후 도보 {formatDistance(option.walkingDistanceMeters)}
        {option.walkingDurationSeconds != null && ` · 약 ${formatDuration(option.walkingDurationSeconds)}`}</span>}
    {option.reasonText && <span className={s.routeReason}>{displayText(option.reasonText)}</span>}
  </>;
}

function busSequence(option: RouteOption) {
  return option.routeNames.map(name => formatBusLabel(name).replace(/ 버스$/, '')).join(' → ');
}

function GroupedRouteCard({cards, alight, recommended, selected, busy, simulate, onChoose}: {
  cards: RouteCardData[];
  alight: StationRef;
} & Pick<Props, 'recommended' | 'selected' | 'busy' | 'simulate' | 'onChoose'>) {
  const [busChoice, setBusChoice] = useState<string | null>(null);
  const [showAllBuses, setShowAllBuses] = useState(false);
  const active = cards.find(card => card.option.routeOptionId === (busChoice ?? selected))
    ?? cards.find(card => card.option.journeyStatus === 'ready') ?? cards[0];
  const {option} = active;
  const timing = routeTiming(option, simulate);
  const sequence = busSequence(option);
  const visibleBuses = showAllBuses ? cards : cards.slice(0, 6);
  const busChoices = visibleBuses.includes(active) ? visibleBuses : [...visibleBuses.slice(0, 5), active];
  const heading = `${busSequence(cards[0].option)} 등 ${cards.length}개 · ${stationLabel(alight)} 하차`;

  return <div className={`${s.comparisonCard} ${s.groupedRouteCard} ${recommended ? s.recommendedCard : ''}`}
    role="group" aria-label={heading}>
    <RouteCardHeader names={option.routeNames} transferCount={option.transferCount} duration={timing.total ?? timing.movement}
      arrival={active.arrival}
      busChoices={<span className={s.groupedBusChoices} role="group" aria-label="이용할 버스 선택">
        {busChoices.map(card => <button key={card.option.routeOptionId} type="button" className={s.groupedBusChoice}
          aria-pressed={card === active} disabled={busy || card.option.journeyStatus !== 'ready'}
          onClick={() => setBusChoice(card.option.routeOptionId)}>{busSequence(card.option)}</button>)}
        {cards.length > 6 && <button type="button" className={s.groupedBusChoice}
          aria-expanded={showAllBuses} onClick={() => setShowAllBuses(!showAllBuses)}>
          {showAllBuses ? '접기' : `${cards.length - busChoices.length}개 더 보기`}
        </button>}
      </span>}
      action={<button type="button" className={`${s.cardAction} ${s.groupedRouteAction}`} aria-label={`이 경로 선택하기 · ${sequence}`}
        disabled={busy || option.journeyStatus !== 'ready'} onClick={() => onChoose(option)}>
        이 경로 선택하기 <Icon file="d682b.svg" />
      </button>} />
    <RouteCardStations option={option} />
    <RouteNotes option={option} />
  </div>;
}

export function RouteCards({options, recommended = false, selected, busy, simulate, sortByWait, checkingArrivals, now, arrivals, onChoose}: Props) {
  const busKeys = options.map(option => option.routeNames.length ? JSON.stringify(option.routeNames.map(formatBusLabel)) : null);
  const shortestWalkByBus = new Map<string, number>();
  options.forEach((option, index) => {
    const key = busKeys[index];
    if (key === null) return;
    const best = shortestWalkByBus.get(key);
    if (best === undefined || (option.walkingDistanceMeters ?? Infinity) < (options[best].walkingDistanceMeters ?? Infinity)) {
      shortestWalkByBus.set(key, index);
    }
  });
  const uniqueOptions = options.filter((_, index) => {
    const key = busKeys[index];
    return key === null || shortestWalkByBus.get(key) === index;
  });
  const cards: RouteCardData[] = uniqueOptions.map(option => {
    const snapshot = arrivals?.[option.routeOptionId];
    const boarding = snapshot?.arrivals[0];
    const firstArrival = snapshot?.routeSnapshotVersion === option.routeSnapshotVersion
      && snapshot.arrivals.length === option.routeNames.length
      && (!option.boardStation || boarding?.boardStationId === option.boardStation.stationId)
      ? currentArrival(boarding, now) : null;
    const arrival = firstArrival?.label ?? (simulate && arrivals ? simulatedArrivalLabel(option.routeOptionId, 0)
      : checkingArrivals ? '도착 정보 확인 중' : '도착 정보 확인 불가');
    return {option, arrival, wait: firstArrival?.waitSeconds ?? Infinity};
  });
  if (sortByWait) cards.sort((a, b) => a.wait === b.wait ? 0 : a.wait - b.wait);
  const groups = new Map<string, RouteCardData[]>();
  for (const card of cards) {
    const {option} = card;
    const key = option.pathGroupId && option.routeNames.length && option.boardStation && option.alightStation
      ? JSON.stringify([option.pathGroupId, option.boardStation.stationId, option.alightStation.stationId])
      : option.routeOptionId;
    const group = groups.get(key);
    if (group) group.push(card);
    else groups.set(key, [card]);
  }
  const displayedGroups = [...groups];
  if (recommended) displayedGroups.splice(1);
  return <div className={s.routeList} role="group" aria-label="이용 가능 경로">{displayedGroups.map(([key, group]) => {
    const {option, arrival} = group[0];
    if (group.length > 1 && option.boardStation && option.alightStation) {
      return <GroupedRouteCard key={key} cards={group} alight={option.alightStation}
        recommended={recommended} selected={selected} busy={busy} simulate={simulate} onChoose={onChoose} />;
    }
    const timing = routeTiming(option, simulate);
    return <button key={option.routeOptionId} className={`${s.comparisonCard} ${recommended ? s.recommendedCard : ''}`} aria-pressed={selected === option.routeOptionId}
      disabled={busy || option.journeyStatus !== 'ready'} onClick={() => onChoose(option)}>
      <RouteCardHeader names={option.routeNames} transferCount={option.transferCount} duration={timing.total ?? timing.movement}
        arrival={option.routeNames.length > 0 ? arrival : undefined}
        action={<span className={s.cardAction}>이 경로 선택하기 <Icon file="d682b.svg" /></span>} />
      <RouteCardStations option={option} />
      <RouteNotes option={option} />
    </button>;
  })}</div>;
}
