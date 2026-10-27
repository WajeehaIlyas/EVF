import { useEffect, useRef, useState } from 'react';

import { deleteSite, getErrorMessage, siteXlsxUrl } from '../services/api';

/**
 * Confirm-and-delete dialog for a site.
 *
 *   <DeleteSiteDialog site={{ id, name, code, events, archived }} onClose={...} onDeleted={(result) => ...} />
 *
 * Deleting is permanent: the site, its events and images, the reviews and the analytics history.
 * The user must type the site name, and is offered the .xlsx workbook as a last copy first.
 */
export default function DeleteSiteDialog({ site, onClose, onDeleted }) {
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const input = useRef(null);
  const matches = typed.trim().toLowerCase() === site.name.trim().toLowerCase();

  useEffect(() => { input.current?.focus(); }, []);
  useEffect(() => {
    const onKey = (event) => { if (event.key === 'Escape' && !busy) onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy, onClose]);

  const remove = async (event) => {
    event.preventDefault();
    if (!matches || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await deleteSite(site.id);
      onDeleted?.(result);
    } catch (err) {
      setError(getErrorMessage(err));
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-900/60 sm:items-center sm:p-4" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <form onSubmit={remove} role="alertdialog" aria-modal="true" aria-labelledby="delete-title"
        className="w-full max-w-md rounded-t-2xl border border-slate-200 bg-white p-5 shadow-xl dark:border-slate-700 dark:bg-slate-900 sm:rounded-2xl">
        <h2 id="delete-title" className="text-lg font-semibold text-rose-700 dark:text-rose-400">Delete “{site.name}”?</h2>
        <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
          This permanently removes the site
          {site.events > 0 ? `, its ${site.events.toLocaleString()} events with their images` : ''}, every review
          {site.archived > 0 ? `, and the ${site.archived.toLocaleString()} records that only live in its analytics` : ''}.
          Its analytics disappear too. This cannot be undone.
        </p>
        <p className="mt-2 text-sm">
          Want a copy first?{' '}
          <a href={siteXlsxUrl(site.id)} className="font-medium text-indigo-600 underline dark:text-indigo-400">Download the .xlsx</a>
        </p>

        <label className="mt-4 block text-sm">
          <span className="font-medium">Type <strong>{site.name}</strong> to confirm</span>
          <input ref={input} value={typed} onChange={(e) => setTyped(e.target.value)} disabled={busy} autoComplete="off"
            className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-sm shadow-sm focus:border-rose-500 focus:outline-none focus:ring-2 focus:ring-rose-300 dark:border-slate-600 dark:bg-slate-800" />
        </label>

        {error && <p role="alert" className="mt-3 rounded-lg bg-rose-100 px-3 py-2 text-sm text-rose-900 dark:bg-rose-900/40 dark:text-rose-200">{error}</p>}

        <div className="mt-4 flex justify-end gap-2">
          <button type="button" onClick={onClose} disabled={busy} className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-40 dark:border-slate-600">Cancel</button>
          <button type="submit" disabled={!matches || busy} className="rounded-lg bg-rose-600 px-4 py-1.5 text-sm font-semibold text-white hover:bg-rose-700 disabled:opacity-40">
            {busy ? 'Deleting…' : 'Delete site'}
          </button>
        </div>
      </form>
    </div>
  );
}
