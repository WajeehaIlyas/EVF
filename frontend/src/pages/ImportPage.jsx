import Head from 'next/head';
import { useRouter } from 'next/router';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  cancelImport,
  createImport,
  finishImport,
  getErrorMessage,
  getImport,
  getMetricsSummary,
  getSiteStats,
  listImports,
  uploadImportFiles,
} from '../services/api';
import NavBar from '../components/NavBar';
import { FEATURES } from '../store/reviewStore';
import RequireAuth from '../components/RequireAuth';

const IMAGE_EXTS = ['.jpg', '.jpeg', '.png'];
const MAX_FILE_BYTES = 30 * 1024 * 1024; // must match the server's UPLOAD_MAX_FILE_MB
const BATCH_MAX_FILES = 40;
const BATCH_MAX_BYTES = 20 * 1024 * 1024;
const POLL_MS = 1500;
const DATA_VERSION_KEY = 'evf.dataVersion'; // other open tabs listen for this and refresh
const IMPORT_PREFS_KEY = 'evf.importPrefs';
const FEATURE_LABEL = Object.fromEntries(FEATURES.map((f) => [f.value, f.label]));
const TIMEZONES = [
  'Europe/London', 'UTC', 'Europe/Dublin', 'Europe/Paris', 'Asia/Karachi', 'Asia/Dubai',
  'America/New_York', 'America/Los_Angeles', 'Australia/Sydney',
];

const fieldClass =
  'rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-sm text-slate-900 shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-300 disabled:opacity-50 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100';

// ------------------------------------------------------------------------------ helpers
const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

function extensionOf(name) {
  const dot = name.lastIndexOf('.');
  return dot < 0 ? '' : name.slice(dot).toLowerCase();
}

/** 'json' | 'image' | null (anything else, hidden or half-copied files are ignored). */
function kindOf(file) {
  const name = file.name.toLowerCase();
  if (name.startsWith('.') || name.endsWith('.part') || name.endsWith('.tmp') || name.endsWith('~')) return null;
  const ext = extensionOf(name);
  if (ext === '.json') return 'json';
  return IMAGE_EXTS.includes(ext) ? 'image' : null;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

/** Same rule as the server: 'North Gate #2' -> 'NORTH-GATE-2'. */
function slugifyCode(name) {
  const code = name.trim().replace(/[^A-Za-z0-9]+/g, '-').replace(/^-+|-+$/g, '').toUpperCase().slice(0, 20).replace(/-+$/g, '');
  return code || 'SITE';
}

// ---- file-name keys: mirrors app/services/smart_ingest.py so the preview matches the server
const PLATE_TOKEN = /^(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{3,10}$/;

/** event_1789403042804_864_WA18LXR_... -> { epoch: 1789403042804, track: '864', plate: 'WA18LXR' } */
function parseNameKey(name) {
  const stem = name.replace(/\.[^.]+$/, '');
  const tokens = stem.split(/[_\-\s.]+/).filter(Boolean);
  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    if (/^(\d{10}|\d{13})$/.test(token)) {
      const epoch = Number(token) * (token.length === 10 ? 1000 : 1);
      if (epoch >= 946684800000 && epoch <= 4102444800000) {
        const track = /^\d{1,7}$/.test(tokens[i + 1] || '') ? tokens[i + 1] : null;
        const plate = tokens.slice(i + (track ? 2 : 1), i + (track ? 2 : 1) + 3).find((t) => PLATE_TOKEN.test(t)) || null;
        return { epoch, track, plate };
      }
    }
  }
  return null;
}

const tripleOf = (k) => `${k.epoch}|${k.track ?? ''}|${k.plate ?? ''}`;
const pairOf = (k) => `${k.epoch}|${k.track ?? ''}`;

/** Which feature do the file names say these are (e.g. ..._MobilePhone_...)? */
function detectFeature(names) {
  const counts = {};
  names.slice(0, 80).forEach((name) => {
    const seen = new Set();
    name.replace(/\.[^.]+$/, '').split(/[_\-\s.]+/).forEach((token) => {
      const key = token.toLowerCase();
      if (FEATURE_LABEL[key] && !seen.has(key)) {
        seen.add(key);
        counts[key] = (counts[key] || 0) + 1;
      }
    });
  });
  const best = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
  return best ? best[0] : null;
}

function makeBatches(items) {
  const batches = [];
  let current = { items: [], bytes: 0 };
  items.forEach((item) => {
    const size = item.file.size;
    const full = current.items.length >= BATCH_MAX_FILES
      || (current.items.length > 0 && current.bytes + size > BATCH_MAX_BYTES);
    if (full) {
      batches.push(current);
      current = { items: [], bytes: 0 };
    }
    current.items.push(item);
    current.bytes += size;
  });
  if (current.items.length) batches.push(current);
  return batches;
}

function readJson(key) {
  try {
    return JSON.parse(window.localStorage.getItem(key) || '{}');
  } catch {
    return {};
  }
}

function patchPrefs(key, patch) {
  try {
    window.localStorage.setItem(key, JSON.stringify({ ...readJson(key), ...patch }));
  } catch { /* storage unavailable */ }
}

function formatTime(iso) {
  if (!iso) return '—';
  const stamp = /[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`; // the API sends UTC without a zone
  return new Date(stamp).toLocaleString();
}

function Spinner({ className = 'h-5 w-5' }) {
  return (
    <span
      role="status"
      className={`inline-block animate-spin rounded-full border-2 border-slate-300 border-t-indigo-500 dark:border-slate-600 ${className}`}
    >
      <span className="sr-only">Working…</span>
    </span>
  );
}

function Step({ number, title, children }) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-800 dark:text-slate-100">
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-indigo-600 text-xs font-bold text-white">{number}</span>
        {title}
      </h2>
      <div className="mt-4">{children}</div>
    </section>
  );
}

function Button({ variant = 'primary', busy = false, children, ...props }) {
  const styles = variant === 'primary'
    ? 'bg-indigo-600 text-white hover:bg-indigo-500'
    : 'border border-slate-300 text-slate-700 hover:bg-slate-100 dark:border-slate-600 dark:text-slate-200 dark:hover:bg-slate-800';
  return (
    <button
      type="button"
      {...props}
      className={`inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-50 ${styles}`}
    >
      {busy && <Spinner className="h-4 w-4" />}
      {children}
    </button>
  );
}

function Bar({ value, label }) {
  return (
    <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(value)}
      className="h-2.5 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700">
      <div className="h-full rounded-full bg-indigo-500 transition-all duration-300" style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
    </div>
  );
}

const STATUS_STYLE = {
  completed: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-300',
  failed: 'bg-rose-100 text-rose-800 dark:bg-rose-900/50 dark:text-rose-300',
  default: 'bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-300',
};

// -------------------------------------------------------------------------------- page
function ImportPageInner() {
  const router = useRouter();

  const [sites, setSites] = useState([]);
  const [sitesStatus, setSitesStatus] = useState('loading'); // loading | ready | error
  const [sitesError, setSitesError] = useState(null);
  const [recent, setRecent] = useState([]);

  // step 1: the folders / files the user picked
  // folder = { id, name, role: 'full' | 'crop', items: [{ file, path, kind }], ignored, tooLarge }
  const [folders, setFolders] = useState([]);
  const [selectionNote, setSelectionNote] = useState(null);
  const [peek, setPeek] = useState(null);
  // step 2: destination
  const [mode, setMode] = useState('new'); // 'new' | 'existing'
  const [siteId, setSiteId] = useState('');
  const [name, setName] = useState('');
  const [code, setCode] = useState('');
  const [forceNew, setForceNew] = useState(false);
  const [feature, setFeature] = useState('');
  const [featureTouched, setFeatureTouched] = useState(false);
  const [timezone, setTimezone] = useState('Europe/London');
  // step 3: run
  const [phase, setPhase] = useState('idle'); // idle | uploading | processing | done | error
  const [progress, setProgress] = useState({ sentBytes: 0, totalBytes: 0, sentFiles: 0, totalFiles: 0 });
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);
  const [after, setAfter] = useState(null); // metrics after the import

  const folderInput = useRef(null);
  const filesInput = useRef(null);
  const abortRef = useRef(null);
  const cancelledRef = useRef(false);
  const mountedRef = useRef(true);
  const nextFolderId = useRef(1);

  const busy = phase === 'uploading' || phase === 'processing';

  // ---- setup ---------------------------------------------------------------------------------
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    const el = folderInput.current;
    if (el) {
      el.setAttribute('webkitdirectory', '');
      el.setAttribute('directory', '');
    }
  }, []);

  const refreshSites = useCallback(async () => {
    try {
      const data = await getSiteStats();
      if (!mountedRef.current) return;
      setSites(data);
      setSitesStatus('ready');
      setSitesError(null);
    } catch (err) {
      if (!mountedRef.current) return;
      setSitesError(getErrorMessage(err));
      setSitesStatus('error');
    }
  }, []);

  const refreshRecent = useCallback(async () => {
    try {
      const data = await listImports({ limit: 8 });
      if (mountedRef.current) setRecent(data);
    } catch { /* the history list is optional */ }
  }, []);

  useEffect(() => {
    refreshSites();
    refreshRecent();
  }, [refreshSites, refreshRecent]);

  // remembered feature / time zone, and ?site=ID&feature=... links (e.g. "add more data" from Analytics)
  useEffect(() => {
    if (!router.isReady) return;
    const prefs = readJson(IMPORT_PREFS_KEY);
    const { site, feature: queryFeature } = router.query;
    if (site) {
      setMode('existing');
      setSiteId(String(site));
    }
    const wanted = queryFeature || prefs.feature;
    if (wanted && FEATURE_LABEL[wanted]) setFeature(wanted);
    if (prefs.timezone) setTimezone(prefs.timezone);
  }, [router.isReady, router.query]);

  useEffect(() => {
    if (!busy) return undefined;
    const warn = (event) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [busy]);

  // ---- step 1: choosing folders / files -----------------------------------------------------------
  const handleSelection = (fileList) => {
    const all = Array.from(fileList || []);
    if (!all.length) return;

    const items = [];
    const tooLarge = [];
    let ignored = 0;
    all.forEach((file) => {
      const kind = kindOf(file);
      if (!kind) { ignored += 1; return; }
      if (file.size > MAX_FILE_BYTES) { tooLarge.push(file.name); return; }
      const parts = (file.webkitRelativePath || '').split('/').filter(Boolean);
      items.push({ file, kind, path: parts.length > 1 ? parts.slice(1).join('/') : file.name });
    });

    const first = all[0].webkitRelativePath || '';
    const folderName = first.includes('/') ? first.split('/')[0] : 'Selected files';
    if (!items.length) {
      setSelectionNote(`“${folderName}” has no .json or image (.jpg/.png) files.`);
      return;
    }
    setSelectionNote(null);
    setFolders((prev) => [...prev, {
      id: nextFolderId.current++,
      name: folderName,
      role: /crop/i.test(folderName) ? 'crop' : 'full',
      items, ignored, tooLarge,
    }]);
    setPhase('idle');
    setJob(null);
    setAfter(null);
    setError(null);
    setPeek(null);
  };

  const removeFolder = (id) => {
    setFolders((prev) => prev.filter((f) => f.id !== id));
    setPeek(null);
  };

  const setFolderRole = (id, role) => {
    setFolders((prev) => prev.map((f) => (f.id === id ? { ...f, role } : f)));
  };

  // flatten everything with the server-side role each file will get
  const allItems = useMemo(() => folders.flatMap((folder) => folder.items.map((item) => ({
    ...item,
    stagedRole: item.kind === 'json' ? 'data' : folder.role,
  }))), [folders]);

  const dataItems = useMemo(() => allItems.filter((i) => i.stagedRole === 'data'), [allItems]);
  const fullItems = useMemo(() => allItems.filter((i) => i.stagedRole === 'full'), [allItems]);
  const cropItems = useMemo(() => allItems.filter((i) => i.stagedRole === 'crop'), [allItems]);
  const totalBytes = useMemo(() => allItems.reduce((sum, i) => sum + i.file.size, 0), [allItems]);

  // what the server is going to do with this selection (same matching rules as the server)
  const preview = useMemo(() => {
    const byTriple = new Map();
    const byPair = new Map();
    const byStem = new Map();
    let unreadable = 0;
    fullItems.forEach((item, index) => {
      const key = parseNameKey(item.file.name);
      byStem.set(item.file.name.replace(/\.[^.]+$/, '').toLowerCase(), index);
      if (!key) { unreadable += 1; return; }
      if (!byTriple.has(tripleOf(key))) byTriple.set(tripleOf(key), index);
      if (key.track && !byPair.has(pairOf(key))) byPair.set(pairOf(key), index);
    });
    const used = new Set();
    let matched = 0;
    dataItems.forEach((item) => {
      const key = parseNameKey(item.file.name);
      const hit = (key && (byTriple.get(tripleOf(key)) ?? (key.track ? byPair.get(pairOf(key)) : undefined)))
        ?? byStem.get(item.file.name.replace(/\.[^.]+$/, '').toLowerCase());
      if (hit !== undefined) { matched += 1; used.add(hit); }
    });
    return {
      matched,
      dataWithoutImage: dataItems.length - matched,
      imagesOnly: fullItems.length - used.size,
      unreadableImages: unreadable,
    };
  }, [dataItems, fullItems]);

  // suggest the feature named in the files (…_MobilePhone_…) unless the user already chose one
  const detectedFeature = useMemo(() => detectFeature(allItems.map((i) => i.file.name)), [allItems]);
  useEffect(() => {
    if (detectedFeature && !featureTouched) setFeature(detectedFeature);
  }, [detectedFeature, featureTouched]);

  const showPeek = async () => {
    const first = dataItems[0];
    if (!first) return;
    try {
      setPeek({ name: first.file.name, text: await first.file.slice(0, 4000).text() });
    } catch {
      setPeek({ name: first.file.name, text: 'Could not read this file.' });
    }
  };

  // ---- step 2: destination ---------------------------------------------------------------------
  const conflict = useMemo(() => {
    if (mode !== 'new' || !name.trim()) return null;
    const typedName = name.trim().toLowerCase();
    const typedCode = (code.trim() || slugifyCode(name)).toLowerCase();
    return sites.find((s) => s.name.toLowerCase() === typedName || (code.trim() && s.code.toLowerCase() === typedCode)) || null;
  }, [mode, name, code, sites]);

  const chosenSite = sites.find((s) => String(s.id) === String(siteId));

  const destinationReady = mode === 'existing'
    ? Boolean(chosenSite)
    : Boolean(name.trim()) && (!conflict || forceNew);

  const canStart = !busy && fullItems.length > 0 && Boolean(feature) && Boolean(timezone.trim()) && destinationReady;

  // ---- step 3: upload + ingest --------------------------------------------------------------------
  const startImport = async () => {
    setError(null);
    setAfter(null);
    setPhase('uploading');
    cancelledRef.current = false;
    const controller = new AbortController();
    abortRef.current = controller;
    patchPrefs(IMPORT_PREFS_KEY, { feature, timezone });

    const uploadItems = allItems.map((item) => ({ file: item.file, path: `${item.stagedRole}/${item.path}` }));
    const total = uploadItems.reduce((sum, i) => sum + i.file.size, 0);
    setProgress({ sentBytes: 0, totalBytes: total, sentFiles: 0, totalFiles: uploadItems.length });

    let created = null;
    try {
      created = await createImport(
        mode === 'existing'
          ? { feature, site_id: Number(siteId), timezone: timezone.trim() }
          : { feature, new_site: { name: name.trim(), code: code.trim() || null }, timezone: timezone.trim() },
      );
      setJob(created);

      let sentBytes = 0;
      let sentFiles = 0;
      for (const batch of makeBatches(uploadItems)) {
        await uploadImportFiles(created.id, batch.items, {
          signal: controller.signal,
          onProgress: (loaded) => setProgress({
            sentBytes: sentBytes + Math.min(loaded, batch.bytes), totalBytes: total, sentFiles, totalFiles: uploadItems.length,
          }),
        });
        sentBytes += batch.bytes;
        sentFiles += batch.items.length;
        setProgress({ sentBytes, totalBytes: total, sentFiles, totalFiles: uploadItems.length });
      }

      setPhase('processing');
      let current = await finishImport(created.id);
      setJob(current);
      while (current.status === 'queued' || current.status === 'processing') {
        await sleep(POLL_MS);
        if (!mountedRef.current) return;
        current = await getImport(created.id);
        setJob(current);
      }

      if (current.status === 'failed') {
        setError(current.error || 'The server could not process these files.');
        setPhase('error');
      } else {
        await onCompleted(current);
      }
    } catch (err) {
      if (cancelledRef.current) return;
      setError(getErrorMessage(err));
      setPhase('error');
      if (created) cancelImport(created.id).catch(() => {}); // best effort: frees the staged files
    } finally {
      refreshRecent();
      refreshSites();
    }
  };

  const onCompleted = async (finished) => {
    setJob(finished);
    setPhase('done');
    // tell other open tabs (Review / Analytics) that this site has new data
    try { window.localStorage.setItem(DATA_VERSION_KEY, String(Date.now())); } catch { /* ignore */ }
    try {
      setAfter(await getMetricsSummary(finished.site_id, finished.feature));
    } catch { /* the summary is a nicety */ }
  };

  const cancelUpload = async () => {
    cancelledRef.current = true;
    abortRef.current?.abort();
    if (job?.id) cancelImport(job.id).catch(() => {});
    setPhase('idle');
    setError('Upload cancelled. Nothing was imported.');
  };

  const reset = () => {
    setFolders([]);
    setSelectionNote(null);
    setPeek(null);
    setPhase('idle');
    setJob(null);
    setAfter(null);
    setError(null);
    setForceNew(false);
    setFeatureTouched(false);
    setName('');
    setCode('');
  };

  const goReview = () => {
    patchPrefs('evf.reviewPrefs', { siteId: job.site_id, feature: job.feature });
    router.push('/');
  };
  const goAnalytics = () => {
    patchPrefs('evf.analyticsPrefs', { siteId: job.site_id, feature: job.feature });
    router.push('/analytics');
  };

  const percent = progress.totalBytes ? (progress.sentBytes / progress.totalBytes) * 100 : 0;
  const featureMismatch = detectedFeature && feature && detectedFeature !== feature;

  // ---- render ---------------------------------------------------------------------------------------
  return (
    <>
      <Head>
        <title>Import data · EVF</title>
      </Head>

      <div className="min-h-screen bg-slate-100 pb-16 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
        <NavBar current="import" />

        <main className="mx-auto max-w-4xl space-y-4 p-3">
          {sitesStatus === 'error' && (
            <p className="rounded-lg bg-rose-100 px-3 py-2 text-sm text-rose-900 dark:bg-rose-900/40 dark:text-rose-200">
              Couldn&apos;t load your sites: {sitesError}{' '}
              <button type="button" className="font-semibold underline" onClick={refreshSites}>Retry</button>
            </p>
          )}

          {/* ---------------------------------------------------------------- done */}
          {phase === 'done' && job && (
            <section className="rounded-xl border border-emerald-300 bg-white p-5 shadow-sm dark:border-emerald-700 dark:bg-slate-900" role="status">
              <h2 className="text-lg font-semibold text-emerald-700 dark:text-emerald-400">
                Import complete{job.created_site ? ' — new site created' : ' — added to existing site'}
              </h2>
              <p className="mt-1 text-sm text-slate-600 dark:text-slate-300">
                {job.site_name} ({job.site_code}) · {FEATURE_LABEL[job.feature] || job.feature}
              </p>

              <dl className="mt-4 grid grid-cols-3 gap-3 text-center">
                {[
                  ['New events', job.events_created, 'text-emerald-600 dark:text-emerald-400'],
                  ['Already present (skipped)', job.duplicates_skipped, 'text-slate-700 dark:text-slate-200'],
                  ['Could not import', job.invalid_records, job.invalid_records ? 'text-rose-600 dark:text-rose-400' : 'text-slate-700 dark:text-slate-200'],
                ].map(([label, value, tone]) => (
                  <div key={label} className="rounded-lg bg-slate-50 p-3 dark:bg-slate-800">
                    <dt className="text-[11px] uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</dt>
                    <dd className={`mt-1 text-2xl font-bold tabular-nums ${tone}`}>{value.toLocaleString()}</dd>
                  </div>
                ))}
              </dl>
              <p className="mt-3 text-xs text-slate-600 dark:text-slate-300">
                {job.events_from_json.toLocaleString()} built from a data file and its image ·{' '}
                {job.events_images_only.toLocaleString()} from an image alone (no data file)
              </p>

              {after && (
                <p className="mt-4 rounded-lg bg-indigo-50 px-3 py-2 text-sm text-indigo-900 dark:bg-indigo-900/30 dark:text-indigo-200">
                  Analysis updated: this selection now holds <strong>{after.total_events.toLocaleString()}</strong> events,{' '}
                  <strong>{after.total_reviewed.toLocaleString()}</strong> reviewed and{' '}
                  <strong>{after.pending_review.toLocaleString()}</strong> waiting for review.
                </p>
              )}

              {job.issues?.length > 0 && (
                <details className="mt-3 text-xs text-slate-600 dark:text-slate-300" open={job.events_created === 0}>
                  <summary className="cursor-pointer font-medium">Show problems ({job.issues.length})</summary>
                  <ul className="mt-2 list-disc space-y-1 break-all pl-5">
                    {job.issues.map((issue, i) => (
                      // eslint-disable-next-line react/no-array-index-key
                      <li key={i}><code>{issue.source}</code>: {issue.message}</li>
                    ))}
                  </ul>
                </details>
              )}

              <div className="mt-5 flex flex-wrap gap-2">
                <Button onClick={goReview} disabled={job.events_created === 0 && job.duplicates_skipped === 0}>Review these events</Button>
                <Button variant="secondary" onClick={goAnalytics}>View analytics</Button>
                <Button variant="secondary" onClick={reset}>Import more</Button>
              </div>
            </section>
          )}

          {phase !== 'done' && (
            <>
              {/* ------------------------------------------------------- step 1 */}
              <Step number={1} title="Choose your folders">
                <p className="mb-3 text-xs text-slate-500 dark:text-slate-400">
                  Pick <strong>one or two folders</strong>: usually one with the data files (<code>.json</code>) and one with the
                  full images (<code>.jpg/.png</code>). Images on their own also work. Files are matched by the capture time,
                  track id and plate in their names.
                </p>

                <div className="flex flex-wrap gap-2">
                  <Button onClick={() => folderInput.current?.click()} disabled={busy}>
                    {folders.length ? 'Add another folder…' : 'Choose a folder…'}
                  </Button>
                  <Button variant="secondary" onClick={() => filesInput.current?.click()} disabled={busy}>Choose files…</Button>
                  <input ref={folderInput} type="file" multiple className="hidden"
                    onChange={(e) => { handleSelection(e.target.files); e.target.value = ''; }} />
                  <input ref={filesInput} type="file" multiple className="hidden"
                    accept=".json,.jpg,.jpeg,.png"
                    onChange={(e) => { handleSelection(e.target.files); e.target.value = ''; }} />
                </div>

                {selectionNote && (
                  <p className="mt-2 text-sm text-amber-700 dark:text-amber-400" role="alert">{selectionNote}</p>
                )}

                {folders.length > 0 && (
                  <ul className="mt-4 space-y-2">
                    {folders.map((folder) => {
                      const jsonCount = folder.items.filter((i) => i.kind === 'json').length;
                      const imageCount = folder.items.length - jsonCount;
                      const bytes = folder.items.reduce((sum, i) => sum + i.file.size, 0);
                      return (
                        <li key={folder.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg bg-slate-50 px-3 py-2 text-sm dark:bg-slate-800">
                          <span className="min-w-0 flex-1">
                            <strong className="break-all">{folder.name}</strong>
                            <span className="text-slate-600 dark:text-slate-300">
                              {' '}· {jsonCount > 0 && `${jsonCount.toLocaleString()} data files`}
                              {jsonCount > 0 && imageCount > 0 && ', '}
                              {imageCount > 0 && `${imageCount.toLocaleString()} images`} · {formatBytes(bytes)}
                            </span>
                            {folder.ignored > 0 && <span className="text-xs text-slate-500"> · {folder.ignored} other files ignored</span>}
                            {folder.tooLarge.length > 0 && (
                              <span className="block text-xs text-amber-700 dark:text-amber-400">
                                {folder.tooLarge.length} file(s) over {MAX_FILE_BYTES / 1024 / 1024} MB will be skipped
                              </span>
                            )}
                          </span>
                          {imageCount > 0 && (
                            <label className="flex items-center gap-1.5 text-xs text-slate-600 dark:text-slate-300">
                              Images are
                              <select value={folder.role} disabled={busy} aria-label={`Role of images in ${folder.name}`}
                                onChange={(e) => setFolderRole(folder.id, e.target.value)}
                                className={`${fieldClass} py-1 text-xs`}>
                                <option value="full">Full images</option>
                                <option value="crop">Crop images</option>
                              </select>
                            </label>
                          )}
                          <button type="button" disabled={busy} onClick={() => removeFolder(folder.id)}
                            className="text-xs font-medium text-rose-600 hover:underline disabled:opacity-50 dark:text-rose-400">
                            Remove
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                )}

                {folders.length > 0 && (
                  <div className="mt-3 space-y-1 rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-sm text-indigo-900 dark:border-indigo-800 dark:bg-indigo-900/20 dark:text-indigo-200">
                    <p className="font-medium">What will be imported</p>
                    {fullItems.length === 0 ? (
                      <p className="text-rose-700 dark:text-rose-300">
                        No full images yet. Each event is reviewed on its full image, so add the folder that holds them.
                      </p>
                    ) : (
                      <ul className="list-disc space-y-0.5 pl-5 text-xs">
                        <li>{fullItems.length.toLocaleString()} full images to review{cropItems.length > 0 && `, plus ${cropItems.length.toLocaleString()} crops`}</li>
                        {dataItems.length > 0 && (
                          <li>{preview.matched.toLocaleString()} of {dataItems.length.toLocaleString()} data files have a matching image</li>
                        )}
                        {preview.dataWithoutImage > 0 && (
                          <li className="text-amber-800 dark:text-amber-300">
                            {preview.dataWithoutImage.toLocaleString()} data files have no matching image and will be reported as problems
                          </li>
                        )}
                        {preview.imagesOnly > 0 && (
                          <li>
                            {preview.imagesOnly.toLocaleString()} images have no data file: they are imported as images only
                            (plate and time come from the file name; no vehicle make/model/colour)
                          </li>
                        )}
                        {preview.unreadableImages > 0 && (
                          <li className="text-amber-800 dark:text-amber-300">
                            {preview.unreadableImages.toLocaleString()} image names have no readable time, so they can only be used when a data file points to them
                          </li>
                        )}
                      </ul>
                    )}
                    <p className="text-xs opacity-80">{allItems.length.toLocaleString()} files · {formatBytes(totalBytes)} to upload</p>
                  </div>
                )}

                {dataItems.length > 0 && (
                  <div className="mt-2">
                    {peek ? (
                      <details open className="text-xs">
                        <summary className="cursor-pointer font-medium text-slate-600 dark:text-slate-300">First data file: {peek.name}</summary>
                        <pre className="mt-1 max-h-56 overflow-auto rounded bg-slate-100 p-2 text-[11px] dark:bg-slate-800">{peek.text}</pre>
                      </details>
                    ) : (
                      <button type="button" onClick={showPeek} className="text-xs font-medium text-indigo-600 hover:underline dark:text-indigo-400">
                        Peek at the first data file
                      </button>
                    )}
                  </div>
                )}
              </Step>

              {/* ------------------------------------------------------- step 2 */}
              <Step number={2} title="Where should it go?">
                <fieldset className="space-y-3" disabled={busy}>
                  <legend className="sr-only">Destination</legend>

                  <label className="flex cursor-pointer items-start gap-2">
                    <input type="radio" name="mode" className="mt-1" checked={mode === 'new'} onChange={() => setMode('new')} />
                    <span className="flex-1">
                      <span className="text-sm font-medium">Create a new site</span>
                      {mode === 'new' && (
                        <span className="mt-2 block space-y-2">
                          <input
                            type="text"
                            value={name}
                            maxLength={255}
                            onChange={(e) => { setName(e.target.value); setForceNew(false); }}
                            placeholder="Name this site, e.g. Exeter"
                            aria-label="Site name"
                            className={`${fieldClass} block w-full`}
                          />
                          <span className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
                            Code
                            <input
                              type="text"
                              value={code}
                              maxLength={100}
                              onChange={(e) => { setCode(e.target.value); setForceNew(false); }}
                              placeholder={name.trim() ? slugifyCode(name) : 'generated from the name'}
                              aria-label="Site code"
                              className={`${fieldClass} w-48 font-mono`}
                            />
                            <span>optional; used for hot-folder names</span>
                          </span>

                          {conflict && !forceNew && (
                            <span className="block rounded-lg bg-amber-100 px-3 py-2 text-sm text-amber-900 dark:bg-amber-900/40 dark:text-amber-200" role="alert">
                              A site called <strong>{conflict.name}</strong> ({conflict.code}) already exists with{' '}
                              {conflict.event_count.toLocaleString()} events. What would you like to do?
                              <span className="mt-2 flex flex-wrap gap-2">
                                <Button onClick={() => { setMode('existing'); setSiteId(String(conflict.id)); }}>
                                  Add to “{conflict.name}”
                                </Button>
                                <Button variant="secondary" onClick={() => setForceNew(true)}>Create a separate site</Button>
                              </span>
                            </span>
                          )}
                          {conflict && forceNew && (
                            <span className="block text-xs text-slate-500 dark:text-slate-400">
                              A separate site will be created. Give it a different name or code so you can tell them apart.
                            </span>
                          )}
                        </span>
                      )}
                    </span>
                  </label>

                  <label className={`flex items-start gap-2 ${sites.length ? 'cursor-pointer' : 'opacity-50'}`}>
                    <input type="radio" name="mode" className="mt-1" disabled={!sites.length}
                      checked={mode === 'existing'} onChange={() => setMode('existing')} />
                    <span className="flex-1">
                      <span className="text-sm font-medium">Add to an existing site</span>
                      {!sites.length && sitesStatus === 'ready' && (
                        <span className="block text-xs text-slate-500 dark:text-slate-400">No sites yet — create the first one above.</span>
                      )}
                      {mode === 'existing' && (
                        <span className="mt-2 block">
                          <select value={siteId} onChange={(e) => setSiteId(e.target.value)} aria-label="Existing site"
                            className={`${fieldClass} block w-full`}>
                            <option value="">Choose a site…</option>
                            {sites.map((s) => (
                              <option key={s.id} value={s.id}>
                                {s.name} ({s.code}) · {s.event_count.toLocaleString()} events · {s.pending_count.toLocaleString()} pending review
                              </option>
                            ))}
                          </select>
                          <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">
                            New events are added alongside the existing ones. Events that are already in the site are
                            skipped, never overwritten, and the analysis updates when the import finishes.
                          </span>
                        </span>
                      )}
                    </span>
                  </label>

                  <div className="grid gap-3 sm:grid-cols-2">
                    <label className="block">
                      <span className="text-sm font-medium">Feature</span>
                      <select value={feature} onChange={(e) => { setFeature(e.target.value); setFeatureTouched(true); }}
                        aria-label="Feature" className={`${fieldClass} mt-1 block w-full`}>
                        <option value="">Choose a feature…</option>
                        {FEATURES.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
                      </select>
                      {detectedFeature && !featureMismatch && (
                        <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">
                          Detected from the file names: {FEATURE_LABEL[detectedFeature]}
                        </span>
                      )}
                      {featureMismatch && (
                        <span className="mt-1 block text-xs text-amber-700 dark:text-amber-400" role="alert">
                          The file names say <strong>{FEATURE_LABEL[detectedFeature]}</strong>, but you chose{' '}
                          <strong>{FEATURE_LABEL[feature]}</strong>.
                        </span>
                      )}
                    </label>

                    <label className="block">
                      <span className="text-sm font-medium">Camera time zone</span>
                      <input type="text" list="timezones" value={timezone} onChange={(e) => setTimezone(e.target.value)}
                        aria-label="Camera time zone" className={`${fieldClass} mt-1 block w-full`} />
                      <datalist id="timezones">
                        {TIMEZONES.map((tz) => <option key={tz} value={tz} />)}
                      </datalist>
                      <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">
                        File-name times are UTC; this decides Day / Night (07:00–18:59) at the camera.
                      </span>
                    </label>
                  </div>
                </fieldset>
              </Step>

              {/* ------------------------------------------------------- step 3 */}
              <Step number={3} title="Import">
                {phase === 'idle' || phase === 'error' ? (
                  <Button onClick={startImport} disabled={!canStart}>
                    {mode === 'existing' ? 'Upload and add to site' : 'Upload and create site'}
                  </Button>
                ) : null}

                {phase === 'uploading' && (
                  <div className="space-y-2">
                    <Bar value={percent} label="Upload progress" />
                    <p className="text-sm text-slate-600 dark:text-slate-300">
                      Uploading {progress.sentFiles.toLocaleString()} / {progress.totalFiles.toLocaleString()} files ·{' '}
                      {formatBytes(progress.sentBytes)} of {formatBytes(progress.totalBytes)}
                    </p>
                    <Button variant="secondary" onClick={cancelUpload}>Cancel</Button>
                  </div>
                )}

                {phase === 'processing' && (
                  <p className="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
                    <Spinner /> Upload finished. The server is matching data to images and updating the analysis…
                  </p>
                )}

                {!canStart && phase === 'idle' && (
                  <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                    {fullItems.length === 0 ? 'Choose a folder with the full images in step 1. ' : ''}
                    {!destinationReady ? 'Finish step 2 (name the site or pick one). ' : ''}
                    {!feature ? 'Choose a feature.' : ''}
                  </p>
                )}

                {error && (
                  <p className="mt-3 rounded-lg bg-rose-100 px-3 py-2 text-sm text-rose-900 dark:bg-rose-900/40 dark:text-rose-200" role="alert">
                    {error}
                  </p>
                )}
              </Step>
            </>
          )}

          {/* ---------------------------------------------------------- history */}
          {recent.length > 0 && (
            <section className="rounded-xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-900">
              <h2 className="px-5 pt-4 text-sm font-semibold text-slate-700 dark:text-slate-200">Recent imports</h2>
              <div className="overflow-x-auto p-5 pt-3">
                <table className="w-full min-w-[560px] text-left text-xs">
                  <thead>
                    <tr className="border-b border-slate-200 uppercase tracking-wide text-slate-500 dark:border-slate-700 dark:text-slate-400">
                      <th className="py-2 pr-3 font-medium">When</th>
                      <th className="px-3 py-2 font-medium">Site</th>
                      <th className="px-3 py-2 font-medium">Feature</th>
                      <th className="px-3 py-2 font-medium">Status</th>
                      <th className="px-3 py-2 text-right font-medium">New</th>
                      <th className="px-3 py-2 text-right font-medium">Skipped</th>
                      <th className="py-2 pl-3 text-right font-medium">Problems</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recent.map((item) => (
                      <tr key={item.id} className="border-b border-slate-100 last:border-0 dark:border-slate-800">
                        <td className="py-2 pr-3 text-slate-600 dark:text-slate-300">{formatTime(item.created_at)}</td>
                        <td className="px-3 py-2 font-medium">{item.site_name} <span className="font-mono text-slate-400">{item.site_code}</span></td>
                        <td className="px-3 py-2">{FEATURE_LABEL[item.feature] || item.feature}</td>
                        <td className="px-3 py-2">
                          <span className={`rounded-full px-2 py-0.5 font-semibold ${STATUS_STYLE[item.status] || STATUS_STYLE.default}`} title={item.error || ''}>
                            {item.status}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums">{item.events_created.toLocaleString()}</td>
                        <td className="px-3 py-2 text-right tabular-nums">{item.duplicates_skipped.toLocaleString()}</td>
                        <td className="py-2 pl-3 text-right tabular-nums">{item.invalid_records.toLocaleString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </main>
      </div>
    </>
  );
}

export default function ImportPage() {
  return (
    <RequireAuth>
      <ImportPageInner />
    </RequireAuth>
  );
}
