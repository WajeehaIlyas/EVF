import { useEffect, useState } from 'react';

import { mediaUrl } from '../services/api';

/**
 * Evidence popup for the Sites table: the full frame of one event, with its details
 * and the reviewer's verdict. Left / right arrows step through the rows on the page. Reviewing is
 * done on the Review page, not here.
 *
 *   <EvidenceViewer rows={items} index={i} onIndex={setI} onClose={...} labelOf={fn} formatTime={fn} />
 */
const VERDICT = {
  TP: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300',
  FP: 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300',
  Unsure: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
};

function tick(value) {
  if (value === true) return '✓';
  if (value === false) return '✗';
  return '–';
}

export default function EvidenceViewer({ rows, index, onIndex, onClose, labelOf = (v) => v, formatTime = (v) => v }) {
  const row = rows[index];
  const [zoom, setZoom] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => { setZoom(false); setFailed(false); }, [row?.id]);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key === 'Escape') onClose();
      else if (event.key === 'ArrowLeft' && index > 0) onIndex(index - 1);
      else if (event.key === 'ArrowRight' && index < rows.length - 1) onIndex(index + 1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [index, rows.length, onClose, onIndex]);

  if (!row) return null;
  const src = mediaUrl(row.full_image_url);
  const review = row.review;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/70 p-2 sm:p-4" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div role="dialog" aria-modal="true" aria-label="Evidence"
        className="flex max-h-[96vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl dark:border-slate-700 dark:bg-slate-900">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
          <div className="mr-auto min-w-0">
            <h2 className="truncate text-base font-semibold">
              {row.vrm || 'No plate read'} <span className="font-normal text-slate-500 dark:text-slate-400">· {labelOf(row.feature)}</span>
            </h2>
            <p className="text-xs text-slate-500 dark:text-slate-400">{formatTime(row.offence_timestamp)} · {row.day_night} · {row.site_name}</p>
          </div>
          <span className={`rounded-full px-2.5 py-0.5 text-xs font-semibold ${review ? VERDICT[review.human_label] : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300'}`}>
            {review ? review.human_label : 'Not reviewed'}
          </span>
          <button type="button" onClick={onClose} aria-label="Close" className="rounded-lg px-2 py-1 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800">✕</button>
        </div>

        <div className="flex items-center px-4 pt-3">
          <a href={src} target="_blank" rel="noreferrer" className="ml-auto text-sm font-medium text-indigo-600 hover:underline dark:text-indigo-400">Open original ↗</a>
        </div>

        <div className="min-h-0 flex-1 overflow-auto px-4 py-3">
          {failed ? (
            <p className="rounded-xl border border-dashed border-slate-300 p-10 text-center text-sm text-slate-500 dark:border-slate-600">
              This image is no longer on the server.
            </p>
          ) : (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={src}
              alt={`Evidence for ${row.vrm || 'the event'}`}
              onError={() => setFailed(true)}
              onClick={() => setZoom((z) => !z)}
              className={`mx-auto rounded-lg bg-slate-100 dark:bg-slate-800 ${zoom ? 'max-w-none cursor-zoom-out' : 'max-h-[58vh] max-w-full cursor-zoom-in object-contain'}`}
            />
          )}
        </div>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 border-t border-slate-200 px-4 py-3 text-sm sm:grid-cols-4 dark:border-slate-700">
          <div><dt className="text-xs text-slate-500">Make</dt><dd>{row.vehicle_make || '—'} {review && <span className="text-slate-500">{tick(review.make_correct)}</span>}</dd></div>
          <div><dt className="text-xs text-slate-500">Model</dt><dd>{row.vehicle_model || '—'} {review && <span className="text-slate-500">{tick(review.model_correct)}</span>}</dd></div>
          <div><dt className="text-xs text-slate-500">Color</dt><dd>{row.vehicle_color || '—'} {review && <span className="text-slate-500">{tick(review.color_correct)}</span>}</dd></div>
          <div><dt className="text-xs text-slate-500">Plate / image</dt><dd>{review ? `${tick(review.vrm_correct)} plate · ${review.image_clarity_clear === false ? 'unclear' : 'clear'}` : '—'}</dd></div>
        </dl>

        <div className="flex items-center justify-between border-t border-slate-200 px-4 py-2 text-sm dark:border-slate-700">
          <button type="button" onClick={() => onIndex(index - 1)} disabled={index <= 0} className="rounded-lg border border-slate-300 px-3 py-1 disabled:opacity-40 dark:border-slate-600">← Previous</button>
          <span className="text-slate-500">{index + 1} of {rows.length} on this page</span>
          <button type="button" onClick={() => onIndex(index + 1)} disabled={index >= rows.length - 1} className="rounded-lg border border-slate-300 px-3 py-1 disabled:opacity-40 dark:border-slate-600">Next →</button>
        </div>
      </div>
    </div>
  );
}
