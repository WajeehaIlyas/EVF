const keepFocus = (event) => event.preventDefault(); // clicks must not steal hotkey focus

const DECISIONS = [
  {
    label: 'TP', name: 'True Positive', keys: ['1', 'T'],
    idle: 'bg-emerald-600 hover:bg-emerald-500 focus-visible:ring-emerald-300',
    flash: 'scale-[0.98] bg-emerald-400',
  },
  {
    label: 'FP', name: 'False Positive', keys: ['2', 'F'],
    idle: 'bg-rose-600 hover:bg-rose-500 focus-visible:ring-rose-300',
    flash: 'scale-[0.98] bg-rose-400',
  },
  {
    label: 'Unsure', name: 'Unsure', keys: ['3', 'U'],
    idle: 'bg-amber-500 hover:bg-amber-400 focus-visible:ring-amber-300',
    flash: 'scale-[0.98] bg-amber-300',
  },
];

function Kbd({ children }) {
  return (
    <kbd className="rounded border border-white/50 bg-black/25 px-1.5 py-0.5 font-mono text-xs font-semibold text-white">
      {children}
    </kbd>
  );
}

function NavButton({ onClick, disabled, label, children }) {
  return (
    <button
      type="button"
      onMouseDown={keepFocus}
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      className="flex h-12 w-12 shrink-0 flex-col items-center justify-center rounded-lg border border-slate-300 text-slate-700 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-30 dark:border-slate-600 dark:text-slate-200 dark:hover:bg-slate-800"
    >
      {children}
    </button>
  );
}

/**
 * Fixed bottom toolbar. `selectedLabel` highlights an earlier decision for the event being
 * revisited; `flash` briefly highlights the button that was just triggered (by key or click).
 */
export default function DecisionBar({
  onDecision,
  onPrevious,
  onNext,
  canPrevious = false,
  canNext = false,
  disabled = false,
  selectedLabel = null,
  flash = null,
  hint = '',
}) {
  return (
    <footer className="fixed inset-x-0 bottom-0 z-30 border-t border-slate-200 bg-white/95 px-3 py-3 backdrop-blur dark:border-slate-700 dark:bg-slate-900/95">
      <div className="mx-auto flex max-w-screen-2xl items-center gap-3">
        <NavButton onClick={onPrevious} disabled={!canPrevious} label="Previous event">
          <span className="text-lg leading-none">←</span>
        </NavButton>

        <div className="min-w-0 flex-1">
          <div className="grid grid-cols-3 gap-3">
            {DECISIONS.map((decision) => {
              const selected = selectedLabel === decision.label;
              const flashing = flash === decision.label;
              return (
                <button
                  key={decision.label}
                  type="button"
                  disabled={disabled}
                  onMouseDown={keepFocus}
                  onClick={() => onDecision(decision.label)}
                  aria-keyshortcuts={decision.keys.join(' ')}
                  aria-pressed={selected}
                  className={`flex h-12 items-center justify-center gap-2 rounded-lg px-3 text-sm font-bold text-white shadow-sm transition duration-100 focus:outline-none focus-visible:ring-4 disabled:cursor-not-allowed disabled:opacity-40 sm:text-base ${
                    flashing ? decision.flash : decision.idle
                  } ${selected ? 'ring-4 ring-indigo-400 ring-offset-2 dark:ring-offset-slate-900' : ''}`}
                >
                  <span>{decision.name}</span>
                  <span className="hidden items-center gap-1 sm:flex">
                    {decision.keys.map((key) => <Kbd key={key}>{key}</Kbd>)}
                  </span>
                </button>
              );
            })}
          </div>
          {hint && (
            <p className="mt-1.5 truncate text-center text-[11px] text-slate-500 dark:text-slate-400">{hint}</p>
          )}
        </div>

        <NavButton onClick={onNext} disabled={!canNext} label="Next event">
          <span className="text-lg leading-none">→</span>
        </NavButton>
      </div>
    </footer>
  );
}
