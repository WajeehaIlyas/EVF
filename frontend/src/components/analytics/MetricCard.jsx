export const TONE_STYLES = {
  good: {
    bar: 'bg-emerald-500',
    text: 'text-emerald-600 dark:text-emerald-400',
    pill: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-300',
    label: 'On target',
  },
  warn: {
    bar: 'bg-amber-500',
    text: 'text-amber-600 dark:text-amber-400',
    pill: 'bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-300',
    label: 'Watch',
  },
  bad: {
    bar: 'bg-rose-500',
    text: 'text-rose-600 dark:text-rose-400',
    pill: 'bg-rose-100 text-rose-800 dark:bg-rose-900/50 dark:text-rose-300',
    label: 'Below target',
  },
  none: {
    bar: 'bg-slate-400',
    text: 'text-slate-500 dark:text-slate-400',
    pill: 'bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300',
    label: 'No data',
  },
};

/** 'good' at/above goodAt, 'warn' at/above warnAt, otherwise 'bad'; 'none' when there is no value. */
export function toneFor(value, goodAt = 90, warnAt = 75) {
  if (value === null || value === undefined) return 'none';
  if (value >= goodAt) return 'good';
  if (value >= warnAt) return 'warn';
  return 'bad';
}

/** 93.456 -> "93.5%"; null/undefined -> "—" (not available, never shown as 0%). */
export function formatPercent(value, digits = 1) {
  return value === null || value === undefined ? '—' : `${value.toFixed(digits)}%`;
}

export function ProgressBar({ value, tone, label }) {
  const width = value === null || value === undefined ? 0 : Math.min(100, Math.max(0, value));
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value ?? undefined}
      className="h-2 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"
    >
      <div
        className={`h-full rounded-full transition-all duration-500 ${TONE_STYLES[tone].bar}`}
        style={{ width: `${width}%` }}
      />
    </div>
  );
}

function DayNightCompare({ day, night, isPercent, goodAt, warnAt }) {
  const rows = [
    { key: 'day', icon: '☀', label: 'Day', value: day },
    { key: 'night', icon: '☾', label: 'Night', value: night },
  ];
  const bothKnown = isPercent && typeof day === 'number' && typeof night === 'number';
  const delta = bothKnown ? night - day : null;

  return (
    <div className="mt-4 space-y-2 border-t border-slate-200 pt-3 dark:border-slate-700">
      {rows.map(({ key, icon, label, value }) => {
        const tone = isPercent ? toneFor(value, goodAt, warnAt) : 'none';
        return (
          <div key={key} className="flex items-center gap-2 text-xs">
            <span className="w-14 shrink-0 text-slate-600 dark:text-slate-300">{icon} {label}</span>
            {isPercent && (
              <div className="flex-1">
                <ProgressBar value={value} tone={tone} label={`${label} ${formatPercent(value)}`} />
              </div>
            )}
            <span className={`w-14 text-right font-semibold tabular-nums ${isPercent ? TONE_STYLES[tone].text : 'ml-auto text-slate-800 dark:text-slate-100'}`}>
              {isPercent ? formatPercent(value) : (value ?? 0).toLocaleString()}
            </span>
          </div>
        );
      })}
      {delta !== null && (
        <p className={`text-[11px] ${Math.abs(delta) >= 5 && delta < 0 ? 'text-amber-600 dark:text-amber-400' : 'text-slate-500 dark:text-slate-400'}`}>
          Night is {Math.abs(delta).toFixed(1)} pts {delta < 0 ? 'below' : delta > 0 ? 'above' : 'level with'} Day
        </p>
      )}
    </div>
  );
}

/**
 * Stat card with a progress indicator and an optional Day vs Night comparison.
 *
 * value:  number | null  (null renders "—" and `emptyText`)
 * format: 'percent' (bar + traffic-light tone) | 'count' (plain number)
 * day/night: pass either to show the comparison (omit both to hide it)
 * goodAt / warnAt: percent thresholds for the green / amber tones
 */
export default function MetricCard({
  title,
  value,
  format = 'percent',
  subtitle,
  detail,
  day,
  night,
  goodAt = 90,
  warnAt = 75,
  emptyText = 'No data yet',
  children,
}) {
  const isPercent = format === 'percent';
  const tone = isPercent ? toneFor(value, goodAt, warnAt) : 'none';
  const hasValue = value !== null && value !== undefined;
  const showCompare = day !== undefined || night !== undefined;

  return (
    <section className="flex flex-col rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <header className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-200">{title}</h3>
          {subtitle && <p className="text-[11px] text-slate-500 dark:text-slate-400">{subtitle}</p>}
        </div>
        {isPercent && (
          <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${TONE_STYLES[tone].pill}`}>
            {TONE_STYLES[tone].label}
          </span>
        )}
      </header>

      <p className={`mt-3 text-3xl font-bold tabular-nums ${isPercent ? TONE_STYLES[tone].text : 'text-slate-900 dark:text-slate-100'}`}>
        {isPercent ? formatPercent(value) : hasValue ? value.toLocaleString() : '—'}
      </p>

      {isPercent && (
        <div className="mt-2">
          <ProgressBar value={value} tone={tone} label={`${title} ${formatPercent(value)}`} />
        </div>
      )}

      {!hasValue && <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">{emptyText}</p>}
      {detail && <p className="mt-2 text-xs text-slate-600 dark:text-slate-300">{detail}</p>}

      {showCompare && (
        <DayNightCompare day={day} night={night} isPercent={isPercent} goodAt={goodAt} warnAt={warnAt} />
      )}

      {children && <div className="mt-3">{children}</div>}
    </section>
  );
}
