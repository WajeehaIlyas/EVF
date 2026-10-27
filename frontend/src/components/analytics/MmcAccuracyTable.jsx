import { ProgressBar, TONE_STYLES, formatPercent, toneFor } from './MetricCard';

const ROWS = [
  { key: 'vrm', label: 'VRM' },
  { key: 'make', label: 'Vehicle Make' },
  { key: 'model', label: 'Vehicle Model' },
  { key: 'color', label: 'Vehicle Color' },
  { key: 'image_clarity', label: 'Image Clarity', note: 'share rated clear' },
];
const COMBINED_ROW = { key: 'mmc', label: 'MMC combined', note: 'make + model + color', emphasis: true };

const STATUS_LABEL = { good: 'Pass', warn: 'Watch', bad: 'Fail', none: 'No data' };

function PctCell({ stat, passAt, warnAt }) {
  const value = stat?.accuracy_pct ?? null;
  const tone = toneFor(value, passAt, warnAt);
  return (
    <span className={`font-semibold tabular-nums ${TONE_STYLES[tone].text}`}>{formatPercent(value)}</span>
  );
}

/**
 * Per-check accuracy: ticks / (ticks + crosses), skipped checks excluded.
 * overall / day / night are the `accuracy` objects from /metrics/summary
 * ({ vrm: { ticks, crosses, checked, accuracy_pct }, make: ..., mmc: ... }).
 * Colours: Pass >= passAt, Watch >= warnAt, Fail below, grey when nothing was checked.
 */
export default function MmcAccuracyTable({
  overall,
  day,
  night,
  passAt = 90,
  warnAt = 75,
  title = 'Check accuracy',
}) {
  const rows = overall?.mmc ? [...ROWS, COMBINED_ROW] : ROWS;

  return (
    <section className="rounded-xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <header className="flex flex-wrap items-baseline justify-between gap-2 px-4 pt-4">
        <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-200">{title}</h3>
        <p className="text-[11px] text-slate-500 dark:text-slate-400">
          Pass ≥ {passAt}% · Watch ≥ {warnAt}% · skipped checks are not counted
        </p>
      </header>

      <div className="overflow-x-auto p-4">
        <table className="w-full min-w-[640px] text-left text-sm">
          <thead>
            <tr className="border-b border-slate-200 text-[11px] uppercase tracking-wide text-slate-500 dark:border-slate-700 dark:text-slate-400">
              <th className="py-2 pr-3 font-medium">Check</th>
              <th className="px-3 py-2 text-right font-medium">✓ Ticks</th>
              <th className="px-3 py-2 text-right font-medium">✕ Crosses</th>
              <th className="px-3 py-2 text-right font-medium">Checked</th>
              <th className="w-44 px-3 py-2 font-medium">Accuracy</th>
              <th className="px-3 py-2 text-right font-medium">☀ Day</th>
              <th className="px-3 py-2 text-right font-medium">☾ Night</th>
              <th className="py-2 pl-3 text-right font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const stat = overall?.[row.key];
              const value = stat?.accuracy_pct ?? null;
              const tone = toneFor(value, passAt, warnAt);
              return (
                <tr
                  key={row.key}
                  className={`border-b border-slate-100 last:border-0 dark:border-slate-800 ${row.emphasis ? 'bg-slate-50 font-medium dark:bg-slate-800/50' : ''}`}
                >
                  <td className="py-2.5 pr-3">
                    <span className="text-slate-900 dark:text-slate-100">{row.label}</span>
                    {row.note && (
                      <span className="ml-2 text-[11px] font-normal text-slate-500 dark:text-slate-400">{row.note}</span>
                    )}
                  </td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-emerald-700 dark:text-emerald-400">
                    {(stat?.ticks ?? 0).toLocaleString()}
                  </td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-rose-700 dark:text-rose-400">
                    {(stat?.crosses ?? 0).toLocaleString()}
                  </td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-slate-600 dark:text-slate-300">
                    {(stat?.checked ?? 0).toLocaleString()}
                  </td>
                  <td className="px-3 py-2.5">
                    <div className="flex items-center gap-2">
                      <div className="flex-1">
                        <ProgressBar value={value} tone={tone} label={`${row.label} accuracy`} />
                      </div>
                      <span className={`w-14 text-right font-semibold tabular-nums ${TONE_STYLES[tone].text}`}>
                        {formatPercent(value)}
                      </span>
                    </div>
                  </td>
                  <td className="px-3 py-2.5 text-right">
                    <PctCell stat={day?.[row.key]} passAt={passAt} warnAt={warnAt} />
                  </td>
                  <td className="px-3 py-2.5 text-right">
                    <PctCell stat={night?.[row.key]} passAt={passAt} warnAt={warnAt} />
                  </td>
                  <td className="py-2.5 pl-3 text-right">
                    <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${TONE_STYLES[tone].pill}`}>
                      {STATUS_LABEL[tone]}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
