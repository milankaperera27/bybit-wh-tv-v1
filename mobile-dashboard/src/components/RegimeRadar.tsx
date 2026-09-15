/**
 * RegimeRadar — real-time 5m / 1h / Daily market-state heatmap (spec §2, agent 1).
 *
 * One column per mandated timeframe, colour-coded by the 4-state regime, with
 * ADX and ATR gauges underneath and an explicit stale-data indicator: a regime
 * cell that stopped updating is far more dangerous than one that admits it is
 * old, because the whole point of the radar is to veto a setup whose regime has
 * already rotated.
 */

import { useEffect, useState } from 'react';

import type { RegimeCell, RegimeSnapshot, Timeframe } from '../api/types';
import { regimeLabel, regimeToken } from '../lib/setup';

/** A cell older than this is called out as stale. */
const STALE_AFTER_MS = 120_000;

/** ADX above this counts as a trending market (matches the Pine default). */
const ADX_TREND_THRESHOLD = 22;
/** Full-scale of the ADX gauge. */
const ADX_MAX = 60;

const TIMEFRAME_ORDER: readonly Timeframe[] = ['5m', '1h', 'D'];
const TIMEFRAME_LABELS: Record<Timeframe, string> = { '5m': '5m', '1h': '1H', D: 'Daily' };

export interface RegimeRadarProps {
  snapshot: RegimeSnapshot | null;
  /** True when the dashboard socket is not currently open. */
  disconnected: boolean;
}

function ageMs(iso: string, now: number): number {
  const parsed = Date.parse(iso);
  return Number.isFinite(parsed) ? Math.max(0, now - parsed) : Number.POSITIVE_INFINITY;
}

function gaugeWidth(value: number, max: number): string {
  if (!Number.isFinite(value) || max <= 0) return '0%';
  return `${Math.min(100, Math.max(0, (value / max) * 100)).toFixed(1)}%`;
}

export function RegimeRadar({ snapshot, disconnected }: RegimeRadarProps): JSX.Element {
  // Local clock so staleness advances even when no new frame arrives.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5_000);
    return () => clearInterval(timer);
  }, []);

  const byTimeframe = new Map<Timeframe, RegimeCell>();
  for (const cell of snapshot?.cells ?? []) {
    byTimeframe.set(cell.timeframe, cell);
  }

  const snapshotAge = snapshot ? ageMs(snapshot.updated_at, now) : Number.POSITIVE_INFINITY;
  const snapshotStale = snapshotAge > STALE_AFTER_MS;

  return (
    <section className="radar" aria-label="Market regime radar">
      <header className="radar__head">
        <h2 className="radar__title">Regime Radar</h2>
        <div className="radar__meta">
          {snapshot !== null && <span className="radar__symbol">{snapshot.symbol}</span>}
          {(snapshotStale || disconnected) && (
            <span className="radar__stale" role="status">
              <span className="radar__stale-dot" aria-hidden="true" />
              {disconnected ? 'Stream down' : 'Stale'}
            </span>
          )}
        </div>
      </header>

      {snapshot === null ? (
        <p className="radar__empty">Waiting for the first regime frame…</p>
      ) : (
        <div className="radar__grid">
          {TIMEFRAME_ORDER.map((timeframe) => {
            const cell = byTimeframe.get(timeframe);
            if (cell === undefined) {
              return (
                <article key={timeframe} className="radar-cell radar-cell--empty">
                  <span className="radar-cell__tf">{TIMEFRAME_LABELS[timeframe]}</span>
                  <span className="radar-cell__regime">No data</span>
                </article>
              );
            }

            const cellAge = ageMs(cell.updated_at, now);
            const stale = cellAge > STALE_AFTER_MS;
            const trending = cell.adx >= ADX_TREND_THRESHOLD;

            return (
              <article
                key={timeframe}
                className={`radar-cell radar-cell--${regimeToken(cell.regime)} ${stale ? 'is-stale' : ''}`}
                title={`${TIMEFRAME_LABELS[timeframe]} · ${regimeLabel(cell.regime)} · updated ${Math.round(cellAge / 1000)}s ago`}
              >
                <div className="radar-cell__top">
                  <span className="radar-cell__tf">{TIMEFRAME_LABELS[timeframe]}</span>
                  {stale && (
                    <span className="radar-cell__staleflag" aria-label="Stale data">
                      !
                    </span>
                  )}
                </div>

                <span className="radar-cell__regime">{regimeLabel(cell.regime)}</span>

                <div className="gauge">
                  <div className="gauge__row">
                    <span className="gauge__label">ADX</span>
                    <span className={`gauge__value ${trending ? 'is-trending' : ''}`}>
                      {cell.adx.toFixed(1)}
                    </span>
                  </div>
                  <div className="gauge__track">
                    <span
                      className={`gauge__fill ${trending ? 'gauge__fill--trend' : ''}`}
                      style={{ width: gaugeWidth(cell.adx, ADX_MAX) }}
                    />
                    <span
                      className="gauge__threshold"
                      style={{ left: gaugeWidth(ADX_TREND_THRESHOLD, ADX_MAX) }}
                      aria-hidden="true"
                    />
                  </div>
                </div>

                <div className="gauge">
                  <div className="gauge__row">
                    <span className="gauge__label">ATR</span>
                    <span className="gauge__value">
                      {cell.atr.toFixed(cell.atr >= 100 ? 1 : 3)}
                      {typeof cell.atr_percentile === 'number' && (
                        <span className="gauge__pct"> p{Math.round(cell.atr_percentile)}</span>
                      )}
                    </span>
                  </div>
                  <div className="gauge__track">
                    <span
                      className="gauge__fill gauge__fill--atr"
                      style={{ width: gaugeWidth(cell.atr_percentile ?? 0, 100) }}
                    />
                  </div>
                </div>

                {typeof cell.cvd_slope === 'number' && (
                  <span className={`radar-cell__cvd ${cell.cvd_slope >= 0 ? 'is-up' : 'is-down'}`}>
                    CVD {cell.cvd_slope >= 0 ? '▲' : '▼'} {Math.abs(cell.cvd_slope).toFixed(2)}
                  </span>
                )}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

export default RegimeRadar;
