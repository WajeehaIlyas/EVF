import { useCallback, useEffect, useRef, useState } from 'react';

import {
  classifiedExportDownloadUrl,
  createClassifiedExport,
  discardClassifiedExport,
  finalizeClassifiedExport,
  getClassifiedPreview,
  getErrorMessage,
} from '../services/api';

/**
 * "Download classified zip" dialog. Works from any page (Analytics, or the Review page's header /
 * "all caught up" screen / pause menu):
 *
 *   <ClassifiedExportDialog
 *     site={{ id: siteId, name: siteName }}
 *     feature={feature}            // 'seatbelt' ... or null for every feature of the site
 *     featureLabel="Seatbelt"      // optional, only used in the heading
 *     onClose={() => setOpen(false)}
 *     onChanged={refreshWhatever}  // called after events were removed from the site
 *   />
 *
 * Steps: 1) name the zip and build it  2) download it  3) once it is saved, optionally remove those
 * events and their images from the site. The analysis keeps every removed event.
 */

const FOLDER_ROWS = [
  ['TP/clear', 'TP', 'clear'],
  ['TP/unclear', 'TP', 'unclear'],
  ['FP/clear', 'FP', 'clear'],
  ['FP/unclear', 'FP', 'unclear'],
  ['Unsure', 'Unsure', 'all'],
];

function formatBytes(bytes) {
  if (bytes < 1024 ** 2) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function FolderTable({ counts }) {
  return (
    <table className="w-full text-sm">
      <tbody>
        {FOLDER_ROWS.map(([key, verdict, clarity]) => (
          <tr key={key} className="border-t border-slate-200 first:border-t-0 dark:border-slate-700">
            <td className="py-1.5 pr-3 font-mono text-xs text-slate-500 dark:text-slate-400">{key}/</td>
            <td className="py-1.5 text-slate-600 dark:text-slate-300">
              {verdict}{clarity === 'all' ? '' : ` · ${clarity}`}
            </td>
            <td className="py-1.5 text-right font-semibold tabular-nums">{(counts?.[key] ?? 0).toLocaleString()}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function ClassifiedExportDialog({ site, feature = null, featureLabel, onClose, onChanged }) {
  const [step, setStep] = useState('form'); // form | building | ready | removing | done
  const [preview, setPreview] = useState(null);
  const [name, setName] = useState('violation-classified');
  const [includeCrops, setIncludeCrops] = useState(false);
  const [built, setBuilt] = useState(null);
  const [downloaded, setDownloaded] = useState(false);
  const [removed, setRemoved] = useState(null);
  const [error, setError] = useState(null);

  const nameInput = useRef(null);
  const tokenRef = useRef(null); // a built zip that has not been finalised yet
  const changedRef = useRef(false);
  const busy = step === 'building' || step === 'removing';

  useEffect(() => {
    let cancelled = false;
    getClassifiedPreview(site.id, feature)
      .then((data) => { if (!cancelled) setPreview(data); })
      .catch((err) => { if (!cancelled) setError(getErrorMessage(err)); });
    return () => { cancelled = true; };
  }, [site.id, feature]);

  useEffect(() => { nameInput.current?.focus(); nameInput.current?.select(); }, []);

  const close = useCallback(() => {
    if (busy) return;
    if (tokenRef.current) {
      discardClassifiedExport(tokenRef.current).catch(() => {}); // the zip is only a temporary copy
      tokenRef.current = null;
    }
    if (changedRef.current) onChanged?.();
    onClose?.();
  }, [busy, onChanged, onClose]);

  useEffect(() => {
    const onKey = (event) => { if (event.key === 'Escape') close(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [close]);

  const build = async (event) => {
    event?.preventDefault();
    setError(null);
    setStep('building');
    try {
      const result = await createClassifiedExport({ siteId: site.id, feature, name, includeCrops });
      tokenRef.current = result.token;
      setBuilt(result);
      setDownloaded(false);
      setStep('ready');
    } catch (err) {
      setError(getErrorMessage(err));
      setStep('form');
    }
  };

  const download = () => {
    const link = document.createElement('a');
    link.href = classifiedExportDownloadUrl(built.token);
    link.download = built.filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setDownloaded(true);
  };

  const removeFromSite = async () => {
    setError(null);
    setStep('removing');
    try {
      const result = await finalizeClassifiedExport(built.token);
      tokenRef.current = null;
      changedRef.current = true;
      setRemoved(result);
      setStep('done');
    } catch (err) {
      setError(getErrorMessage(err));
      setStep('ready');
    }
  };

  const nothingReviewed = preview && preview.reviewed === 0;
  const scope = featureLabel || feature || 'all features';

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-900/60 p-0 sm:items-center sm:p-4" onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="export-title"
        className="max-h-[92vh] w-full max-w-lg overflow-y-auto rounded-t-2xl border border-slate-200 bg-white p-5 shadow-xl dark:border-slate-700 dark:bg-slate-900 sm:rounded-2xl"
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 id="export-title" className="text-lg font-semibold">Download classified images</h2>
            <p className="text-sm text-slate-500 dark:text-slate-400">{site.name} · {scope}</p>
          </div>
          <button type="button" onClick={close} disabled={busy} aria-label="Close" className="rounded-lg px-2 py-1 text-slate-500 hover:bg-slate-100 disabled:opacity-40 dark:hover:bg-slate-800">✕</button>
        </div>

        {error && (
          <p role="alert" className="mt-3 rounded-lg bg-rose-100 px-3 py-2 text-sm text-rose-900 dark:bg-rose-900/40 dark:text-rose-200">{error}</p>
        )}

        {/* ------------------------------------------------------------ form */}
        {(step === 'form' || step === 'building') && (
          <form onSubmit={build} className="mt-4 space-y-4">
            <div className="rounded-xl bg-slate-50 p-3 dark:bg-slate-800/60">
              {preview ? (
                <>
                  <p className="mb-2 text-sm text-slate-600 dark:text-slate-300">
                    <strong>{preview.reviewed.toLocaleString()}</strong> reviewed events will be sorted into folders.
                    {preview.unreviewed > 0 && ` ${preview.unreviewed.toLocaleString()} not reviewed yet stay on the site.`}
                  </p>
                  <FolderTable counts={preview.counts} />
                </>
              ) : (
                <p className="text-sm text-slate-500">Counting reviewed events…</p>
              )}
            </div>

            <label className="block text-sm">
              <span className="font-medium">Zip name</span>
              <div className="mt-1 flex items-center gap-2">
                <input
                  ref={nameInput}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  maxLength={80}
                  disabled={step === 'building'}
                  className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-sm shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-300 dark:border-slate-600 dark:bg-slate-800"
                />
                <span className="text-sm text-slate-500">.zip</span>
              </div>
              <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">The top-level folder inside the zip gets the same name.</span>
            </label>

            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={includeCrops} onChange={(e) => setIncludeCrops(e.target.checked)} disabled={step === 'building'} />
              Also include the crop image next to each full image
            </label>

            <div className="flex justify-end gap-2">
              <button type="button" onClick={close} disabled={busy} className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-40 dark:border-slate-600">Cancel</button>
              <button
                type="submit"
                disabled={busy || !preview || nothingReviewed}
                className="rounded-lg bg-indigo-600 px-4 py-1.5 text-sm font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
              >
                {step === 'building' ? 'Building zip…' : 'Build zip'}
              </button>
            </div>
            {nothingReviewed && (
              <p className="text-sm text-amber-700 dark:text-amber-400">Nothing has been reviewed yet, so there is nothing to download.</p>
            )}
          </form>
        )}

        {/* ------------------------------------------------------------ ready / removing */}
        {(step === 'ready' || step === 'removing') && built && (
          <div className="mt-4 space-y-4">
            <div className="rounded-xl bg-slate-50 p-3 dark:bg-slate-800/60">
              <p className="mb-2 text-sm text-slate-600 dark:text-slate-300">
                <strong>{built.filename}</strong> is ready: {built.events.toLocaleString()} images, {formatBytes(built.zip_bytes)}.
              </p>
              <FolderTable counts={built.counts} />
              {built.missing_images > 0 && (
                <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">
                  {built.missing_images} reviewed event(s) have no image file on the server, so they are not in the zip and will not be removed.
                </p>
              )}
            </div>

            <button type="button" onClick={download} className="w-full rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-700">
              {downloaded ? 'Download again' : `Download ${built.filename}`}
            </button>

            <div className="rounded-xl border border-slate-200 p-3 dark:border-slate-700">
              <p className="text-sm font-medium">Free up space on the site?</p>
              <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                Once you have the zip, these {built.events.toLocaleString()} events and their images can be removed from the site.
                Your analytics keep counting them, and events you have not reviewed are untouched.
              </p>
              <div className="mt-3 flex flex-wrap justify-end gap-2">
                <button type="button" onClick={close} disabled={busy} className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-40 dark:border-slate-600">Keep them on the site</button>
                <button
                  type="button"
                  onClick={removeFromSite}
                  disabled={!downloaded || busy}
                  title={downloaded ? undefined : 'Download the zip first'}
                  className="rounded-lg bg-rose-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-rose-700 disabled:opacity-40"
                >
                  {step === 'removing' ? 'Removing…' : 'I have the zip, remove them'}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* ------------------------------------------------------------ done */}
        {step === 'done' && removed && (
          <div className="mt-4 space-y-3" role="status">
            <p className="text-sm text-emerald-700 dark:text-emerald-400">
              Removed {removed.archived.toLocaleString()} events from the site. The analytics still include them.
            </p>
            {removed.skipped_changed > 0 && (
              <p className="text-sm text-amber-700 dark:text-amber-400">
                {removed.skipped_changed} event(s) were changed after the zip was built, so they were kept. Download again to include them.
              </p>
            )}
            <div className="flex justify-end">
              <button type="button" onClick={close} className="rounded-lg bg-indigo-600 px-4 py-1.5 text-sm font-semibold text-white hover:bg-indigo-700">Done</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
