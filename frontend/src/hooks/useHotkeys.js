/**
 * Global keyboard shortcuts for the review workspace.
 *
 *   1 / T  -> True Positive        2 / F  -> False Positive       3 / U  -> Unsure
 *   <- / -> -> previous / next event
 *   V M O C I -> cycle the VRM / Make / Model / Color / Image-clarity check (blank > tick > cross)
 *   A -> mark all checks as correct      R -> reset checks      S -> toggle "Skip MMC Review"
 *
 * Keys are ignored while typing in a form field, while a modal is open, or when Ctrl/Cmd/Alt is
 * held (so browser shortcuts keep working). Holding a key down does not repeat decisions.
 */
import { useEffect, useRef } from 'react';

const DECISION_KEYS = new Map([
  ['1', 'TP'], ['t', 'TP'],
  ['2', 'FP'], ['f', 'FP'],
  ['3', 'Unsure'], ['u', 'Unsure'],
]);

const TOGGLE_KEYS = new Map([
  ['v', 'vrmCorrect'],
  ['m', 'makeCorrect'],
  ['o', 'modelCorrect'],
  ['c', 'colorCorrect'],
  ['i', 'imageClarityClear'],
]);

/** True when the event target is somewhere the user types or picks options. */
export function isTypingTarget(target) {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
}

/** True when any modal dialog is currently open. */
export function isModalOpen() {
  return Boolean(document.querySelector('[role="dialog"], [aria-modal="true"], dialog[open]'));
}

/**
 * @param {object} handlers  onDecision(label) onPrevious() onNext() onToggle(field)
 *                           onAllCorrect() onResetToggles() onToggleSkipMMC()
 * @param {{enabled?: boolean}} options
 */
export default function useHotkeys(handlers, { enabled = true } = {}) {
  // Keep the latest handlers in a ref so the listener is attached once, not on every render.
  const handlersRef = useRef(handlers);
  useEffect(() => {
    handlersRef.current = handlers;
  });

  useEffect(() => {
    if (!enabled) return undefined;

    const onKeyDown = (event) => {
      if (event.defaultPrevented || event.isComposing) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (isTypingTarget(event.target) || isModalOpen()) return;

      const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
      const h = handlersRef.current;
      let handled = false;

      if (key === 'ArrowLeft') {
        handled = Boolean(h.onPrevious);
        h.onPrevious?.();
      } else if (key === 'ArrowRight') {
        handled = Boolean(h.onNext);
        h.onNext?.();
      } else if (DECISION_KEYS.has(key)) {
        handled = true;
        if (!event.repeat) h.onDecision?.(DECISION_KEYS.get(key));
      } else if (TOGGLE_KEYS.has(key)) {
        handled = true;
        if (!event.repeat) h.onToggle?.(TOGGLE_KEYS.get(key));
      } else if (key === 'a') {
        handled = true;
        if (!event.repeat) h.onAllCorrect?.();
      } else if (key === 'r') {
        handled = true;
        if (!event.repeat) h.onResetToggles?.();
      } else if (key === 's') {
        handled = true;
        if (!event.repeat) h.onToggleSkipMMC?.();
      }

      if (handled) event.preventDefault();
    };

    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [enabled]);
}
