const ROWS = [
  { field: 'vrmCorrect', label: 'VRM', hotkey: 'V', mmc: false,
    getValue: (e) => e.vrm, getConfidence: () => null },
  { field: 'makeCorrect', label: 'Vehicle Make', hotkey: 'M', mmc: true,
    getValue: (e) => e.vehicle_make, getConfidence: (e) => e.make_confidence },
  { field: 'modelCorrect', label: 'Vehicle Model', hotkey: 'O', mmc: true,
    getValue: (e) => e.vehicle_model, getConfidence: (e) => e.model_confidence },
  { field: 'colorCorrect', label: 'Vehicle Color', hotkey: 'C', mmc: true,
    getValue: (e) => e.vehicle_color, getConfidence: (e) => e.color_confidence },
  { field: 'imageClarityClear', label: 'Image Clarity', hotkey: 'I', mmc: false,
    getValue: () => null, placeholder: 'Is the image clear?', getConfidence: () => null },
];

const keepFocus = (event) => event.preventDefault(); // clicks must not steal hotkey focus

function ConfidenceBadge({ value }) {
  if (value === null || value === undefined) return null;
  const pct = Math.round(value * 100);
  const tone =
    pct >= 80
      ? 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-300'
      : pct >= 50
        ? 'bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-300'
        : 'bg-rose-100 text-rose-800 dark:bg-rose-900/50 dark:text-rose-300';
  return (
    <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold tabular-nums ${tone}`}
      title="Model confidence">
      {pct}%
    </span>
  );
}

function ToggleButton({ kind, active, disabled, onClick, label }) {
  const isTick = kind === 'tick';
  const activeClass = isTick
    ? 'border-emerald-600 bg-emerald-600 text-white'
    : 'border-rose-600 bg-rose-600 text-white';
  const idleClass = isTick
    ? 'border-slate-300 text-slate-500 hover:border-emerald-500 hover:text-emerald-600 dark:border-slate-600 dark:text-slate-400'
    : 'border-slate-300 text-slate-500 hover:border-rose-500 hover:text-rose-600 dark:border-slate-600 dark:text-slate-400';
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={active}
      disabled={disabled}
      onMouseDown={keepFocus}
      onClick={onClick}
      className={`flex h-9 w-11 items-center justify-center rounded-md border text-lg font-bold transition focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 disabled:cursor-not-allowed disabled:opacity-40 ${active ? activeClass : idleClass}`}
    >
      {isTick ? '✓' : '✕'}
    </button>
  );
}

/**
 * Tick / cross verification for VRM, make, model, colour and image clarity, plus the
 * "Skip MMC Review" switch. Values: true = tick, false = cross, null = unanswered.
 */
export default function VerificationCard({
  event,
  toggles,
  skipMMC,
  onToggle, // (field, nextValue) => void
  onSkipMMCChange, // (boolean) => void
  missingFields = [],
}) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <header className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-600 dark:text-slate-300">
          Verification
        </h2>
        <div className="flex items-center gap-2">
          <span id="skip-mmc-label" className="text-xs text-slate-600 dark:text-slate-300">
            Skip MMC Review <kbd className="ml-1 rounded border border-slate-300 px-1 font-mono text-[10px] dark:border-slate-600">S</kbd>
          </span>
          <button
            type="button"
            role="switch"
            aria-checked={skipMMC}
            aria-labelledby="skip-mmc-label"
            onMouseDown={keepFocus}
            onClick={() => onSkipMMCChange(!skipMMC)}
            className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400 ${skipMMC ? 'bg-indigo-600' : 'bg-slate-300 dark:bg-slate-600'}`}
          >
            <span className={`inline-block h-5 w-5 rounded-full bg-white shadow transition-transform ${skipMMC ? 'translate-x-5' : 'translate-x-0.5'}`} />
          </button>
        </div>
      </header>

      <ul className="space-y-2">
        {ROWS.map((row) => {
          const locked = skipMMC && row.mmc;
          const value = row.getValue(event);
          const current = toggles[row.field] ?? null;
          const missing = missingFields.includes(row.field);
          return (
            <li
              key={row.field}
              className={`flex items-center gap-3 rounded-lg border px-3 py-2 transition ${
                missing
                  ? 'animate-pulse border-rose-500 bg-rose-50 ring-2 ring-rose-400 dark:bg-rose-950/40'
                  : 'border-slate-200 dark:border-slate-700'
              } ${locked ? 'opacity-50' : ''}`}
            >
              <kbd className="w-5 shrink-0 text-center font-mono text-xs text-slate-500 dark:text-slate-400"
                title="Hotkey: cycles blank → ✓ → ✕">
                {row.hotkey}
              </kbd>

              <div className="min-w-0 flex-1">
                <p className="text-[11px] uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  {row.label}
                </p>
                <p className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">
                  {locked ? (
                    <span className="font-normal italic text-slate-500">skipped</span>
                  ) : value ? (
                    value
                  ) : (
                    <span className="font-normal italic text-slate-500">
                      {row.placeholder || 'not detected'}
                    </span>
                  )}
                </p>
              </div>

              {!locked && <ConfidenceBadge value={row.getConfidence(event)} />}

              <div className="flex gap-1.5">
                <ToggleButton kind="tick" label={`${row.label} correct`} active={current === true}
                  disabled={locked}
                  onClick={() => onToggle(row.field, current === true ? null : true)} />
                <ToggleButton kind="cross" label={`${row.label} incorrect`} active={current === false}
                  disabled={locked}
                  onClick={() => onToggle(row.field, current === false ? null : false)} />
              </div>
            </li>
          );
        })}
      </ul>

      <p className="mt-3 text-[11px] leading-relaxed text-slate-500 dark:text-slate-400">
        <kbd className="font-mono">V M O C I</kbd> cycle blank → ✓ → ✕ · <kbd className="font-mono">A</kbd> all
        correct · <kbd className="font-mono">R</kbd> reset. A True Positive needs every detected
        make / model / color answered unless Skip MMC is on.
      </p>
    </section>
  );
}
