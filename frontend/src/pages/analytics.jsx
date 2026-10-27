import Head from 'next/head';
import Link from 'next/link';
import { useCallback, useEffect, useRef, useState } from 'react';

import ClassifiedExportDialog from '../components/ClassifiedExportDialog';
import DeleteSiteDialog from '../components/DeleteSiteDialog';
import NavBar from '../components/NavBar';
import { ROUTES } from '../lib/routes';
import { getAnalytics, getErrorMessage, importSiteXlsx, siteXlsxUrl } from '../services/api';
import { FEATURES } from '../store/reviewStore';
import RequireAuth from '../components/RequireAuth';

const DATA_VERSION_KEY = 'evf.dataVersion'; // the Import and Review pages bump this after a change
const FEATURE_LABEL = Object.fromEntries(FEATURES.map((f) => [f.value, f.label]));
const labelOf = (feature) => FEATURE_LABEL[feature] || feature;

const nf = new Intl.NumberFormat();
const num = (value) => nf.format(value ?? 0);
const pct = (value) => (value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`);

// ------------------------------------------------------------------------------ small parts
function Tile({ label, value, hint, tone = 'default' }) {
  const toneClass = {
    default: 'text-slate-900 dark:text-slate-100',
    good: 'text-emerald-700 dark:text-emerald-400',
    bad: 'text-rose-700 dark:text-rose-400',
    warn: 'text-amber-700 dark:text-amber-400',
  }[tone];
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-900">
      <dt className="text-xs font-medium text-slate-500 dark:text-slate-400">{label}</dt>
      <dd className={`mt-1 text-2xl font-semibold tabular-nums ${toneClass}`}>{value}</dd>
      {hint && <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">{hint}</p>}
    </div>
  );
}

function Group({ title, children, cols = 'sm:grid-cols-3' }) {
  return (
    <section>
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">{title}</h4>
      <dl className={`grid grid-cols-2 gap-2 ${cols}`}>{children}</dl>
    </section>
  );
}

function SplitBar({ stats }) {
  const total = stats.total_violations || 0;
  const parts = [
    ['True positives', stats.tp, 'bg-emerald-500'],
    ['False positives', stats.fp, 'bg-rose-500'],
    ['Unsure', stats.unsure, 'bg-amber-400'],
    ['Not reviewed', stats.pending, 'bg-slate-300 dark:bg-slate-600'],
  ];
  if (!total) return null;
  return (
    <div>
      <div className="flex h-2.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700" role="img"
        aria-label={parts.map(([n, v]) => `${n}: ${v}`).join(', ')}>
        {parts.map(([name, value, color]) => (
          value > 0 ? <div key={name} className={color} style={{ width: `${(value / total) * 100}%` }} title={`${name}: ${num(value)}`} /> : null
        ))}
      </div>
      <ul className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500 dark:text-slate-400">
        {parts.map(([name, value, color]) => (
          <li key={name} className="flex items-center gap-1.5">
            <span className={`inline-block h-2 w-2 rounded-full ${color}`} aria-hidden="true" />
            {name} {num(value)}
          </li>
        ))}
      </ul>
    </div>
  );
}

function StatsPanel({ stats }) {
  return (
    <div className="space-y-5">
      <Group title="Violations" cols="sm:grid-cols-3 lg:grid-cols-6">
        <Tile label="Total no. violations" value={num(stats.total_violations)} />
        <Tile label="Reviewed events" value={num(stats.reviewed)} hint={stats.total_violations ? `${pct(stats.reviewed / stats.total_violations)} of all` : undefined} />
        <Tile label="Not reviewed yet" value={num(stats.pending)} />
        <Tile label="True positives" value={num(stats.tp)} tone="good" />
        <Tile label="False positives" value={num(stats.fp)} tone="bad" />
        <Tile label="Unsures" value={num(stats.unsure)} tone="warn" />
      </Group>
      <SplitBar stats={stats} />

      <div className="grid gap-5 lg:grid-cols-2">
        <Group title="Image quality (reviewed events)" cols="grid-cols-2">
          <Tile label="Clear images" value={num(stats.clear_images)} />
          <Tile label="Unclear images" value={num(stats.unclear_images)} hint={stats.fp_unclear ? `${num(stats.fp_unclear)} of them false positives` : undefined} />
        </Group>
        <Group title="Precision" cols="grid-cols-2">
          <Tile label="Precision" value={pct(stats.precision)} hint="TP ÷ (TP + FP)" />
          <Tile label="Precision excluding unclear FPs" value={pct(stats.precision_excl_unclear_fp)} hint="TP ÷ (TP + FP − unclear FP)" />
        </Group>
      </div>

      <Group title="MMC (make · model · color)" cols="grid-cols-2 sm:grid-cols-4">
        <Tile label="MMC: Total reviewed events" value={num(stats.mmc_reviewed)} />
        <Tile label="Make accuracy rate" value={pct(stats.make_accuracy)} hint={stats.mmc_reviewed ? `${num(stats.make_correct)} correct` : undefined} />
        <Tile label="Model accuracy rate" value={pct(stats.model_accuracy)} hint={stats.mmc_reviewed ? `${num(stats.model_correct)} correct` : undefined} />
        <Tile label="Color accuracy rate" value={pct(stats.color_accuracy)} hint={stats.mmc_reviewed ? `${num(stats.color_correct)} correct` : undefined} />
      </Group>
    </div>
  );
}

function ImportReport({ report, onDismiss }) {
  return (
    <div className="rounded-xl border border-emerald-300 bg-emerald-50 p-3 text-sm dark:border-emerald-700 dark:bg-emerald-900/20" role="status">
      <div className="flex items-start justify-between gap-3">
        <p className="font-medium text-emerald-800 dark:text-emerald-300">
          Spreadsheet imported: {num(report.added)} added, {num(report.updated)} updated.
        </p>
        <button type="button" onClick={onDismiss} aria-label="Dismiss" className="text-slate-500 hover:text-slate-800 dark:hover:text-slate-200">✕</button>
      </div>
      <ul className="mt-1.5 space-y-1 text-xs text-slate-700 dark:text-slate-300">
        {report.sheets.map((sheet) => (
          <li key={sheet.sheet}>
            <strong>{sheet.sheet}</strong>
            {sheet.skipped_reason
              ? <span className="text-amber-700 dark:text-amber-400"> skipped: {sheet.skipped_reason}</span>
              : (
                <span>
                  {' → '}{labelOf(sheet.feature)}: {num(sheet.added)} added, {num(sheet.updated)} updated, {num(sheet.unchanged)} unchanged
                  {sheet.unlabelled ? `, ${num(sheet.unlabelled)} not labelled yet` : ''}
                  {sheet.already_on_site ? `, ${num(sheet.already_on_site)} already on this site` : ''}
                  {sheet.duplicates ? `, ${num(sheet.duplicates)} duplicate rows` : ''}
                  {sheet.invalid ? `, ${num(sheet.invalid)} unreadable labels` : ''}
                </span>
              )}
            {sheet.problems?.length > 0 && (
              <span className="block text-amber-700 dark:text-amber-400">{sheet.problems.join(' · ')}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

// -------------------------------------------------------------------------------- one site
function SiteCard({ site, onChanged }) {
  const [tab, setTab] = useState('all');
  const [exportOpen, setExportOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [importing, setImporting] = useState(false);
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);
  const fileInput = useRef(null);

  const features = site.features;
  const active = tab === 'all' ? null : features.find((f) => f.feature === tab);
  const stats = active ? active.stats : site.overall;
  const scopeFeature = active ? active.feature : null;
  const empty = site.overall.total_violations === 0;

  const handleFile = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    setError(null);
    setReport(null);
    setImporting(true);
    try {
      const result = await importSiteXlsx(site.site_id, file, { feature: scopeFeature });
      setReport(result);
      try { window.localStorage.setItem(DATA_VERSION_KEY, String(Date.now())); } catch { /* ignore */ }
      await onChanged();
    } catch (err) {
      setError(getErrorMessage(err));
    } finally {
      setImporting(false);
    }
  };

  const tabClass = (selected) => `whitespace-nowrap rounded-full px-3 py-1 text-sm font-medium transition ${
    selected
      ? 'bg-indigo-600 text-white'
      : 'bg-slate-100 text-slate-700 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700'}`;

  return (
    <article className="rounded-2xl border border-slate-200 bg-slate-50/60 p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900/60" aria-labelledby={`site-${site.site_id}`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="mr-auto min-w-0">
          <h2 id={`site-${site.site_id}`} className="truncate text-lg font-semibold">{site.site_name}</h2>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            {site.site_code}
            {site.overall.archived > 0 && ` · ${num(site.overall.archived)} removed from the site or imported from spreadsheets`}
          </p>
        </div>
        <input ref={fileInput} type="file" accept=".xlsx" className="hidden" onChange={handleFile} aria-label={`Import an .xlsx for ${site.site_name}`} />
        <button
          type="button"
          onClick={() => fileInput.current?.click()}
          disabled={importing}
          className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium hover:bg-slate-100 disabled:opacity-50 dark:border-slate-600 dark:bg-slate-800 dark:hover:bg-slate-700"
        >
          {importing ? 'Importing…' : active ? `Import .xlsx into ${labelOf(active.feature)}` : 'Import .xlsx'}
        </button>
        <button
          type="button"
          onClick={() => setExportOpen(true)}
          className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-indigo-700"
        >
          Download classified zip
        </button>
        <a
          href={siteXlsxUrl(site.site_id, scopeFeature)}
          className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium hover:bg-slate-100 dark:border-slate-600 dark:bg-slate-800 dark:hover:bg-slate-700"
          title="Every event of this site as an Excel workbook, laid out like your events sheet"
        >
          Download .xlsx
        </a>
        <Link href={`${ROUTES.import}?site=${site.site_id}${scopeFeature ? `&feature=${scopeFeature}` : ''}`} className="text-sm font-medium text-indigo-600 hover:underline dark:text-indigo-400">
          Add more data
        </Link>
        <Link href={`${ROUTES.sites}?site=${site.site_id}`} className="text-sm font-medium text-indigo-600 hover:underline dark:text-indigo-400">
          View events
        </Link>
        <button type="button" onClick={() => setDeleteOpen(true)} className="text-sm font-medium text-rose-600 hover:underline dark:text-rose-400">
          Delete site
        </button>
      </div>

      {features.length > 0 && (
        <div className="mt-3 flex gap-2 overflow-x-auto pb-1" role="tablist" aria-label="Feature">
          <button type="button" role="tab" aria-selected={tab === 'all'} className={tabClass(tab === 'all')} onClick={() => setTab('all')}>
            All features
          </button>
          {features.map((f) => (
            <button key={f.feature} type="button" role="tab" aria-selected={tab === f.feature} className={tabClass(tab === f.feature)} onClick={() => setTab(f.feature)}>
              {labelOf(f.feature)}
            </button>
          ))}
        </div>
      )}

      {error && <p role="alert" className="mt-3 rounded-lg bg-rose-100 px-3 py-2 text-sm text-rose-900 dark:bg-rose-900/40 dark:text-rose-200">{error}</p>}
      {report && <div className="mt-3"><ImportReport report={report} onDismiss={() => setReport(null)} /></div>}

      <div className="mt-4">
        {empty ? (
          <p className="rounded-xl border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500 dark:border-slate-600 dark:text-slate-400">
            No violations yet. Import images from the Import page, or add a labelled .xlsx above.
          </p>
        ) : (
          <StatsPanel stats={stats} />
        )}
      </div>

      {deleteOpen && (
        <DeleteSiteDialog
          site={{ id: site.site_id, name: site.site_name, events: site.overall.total_violations - site.overall.archived, archived: site.overall.archived }}
          onClose={() => setDeleteOpen(false)}
          onDeleted={() => {
            setDeleteOpen(false);
            try { window.localStorage.setItem(DATA_VERSION_KEY, String(Date.now())); } catch { /* ignore */ }
            onChanged();
          }}
        />
      )}

      {exportOpen && (
        <ClassifiedExportDialog
          site={{ id: site.site_id, name: site.site_name }}
          feature={scopeFeature}
          featureLabel={active ? labelOf(active.feature) : undefined}
          onClose={() => setExportOpen(false)}
          onChanged={() => {
            try { window.localStorage.setItem(DATA_VERSION_KEY, String(Date.now())); } catch { /* ignore */ }
            onChanged();
          }}
        />
      )}
    </article>
  );
}

// ------------------------------------------------------------------------------------ page
function AnalyticsPageInner() {
  const [sites, setSites] = useState(null);
  const [error, setError] = useState(null);
  const mounted = useRef(true);

  const load = useCallback(async () => {
    try {
      const data = await getAnalytics();
      if (!mounted.current) return;
      setSites(data);
      setError(null);
    } catch (err) {
      if (mounted.current) setError(getErrorMessage(err));
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    load();
    const onStorage = (event) => { if (event.key === DATA_VERSION_KEY) load(); };
    const onFocus = () => load();
    window.addEventListener('storage', onStorage);
    window.addEventListener('focus', onFocus);
    return () => {
      mounted.current = false;
      window.removeEventListener('storage', onStorage);
      window.removeEventListener('focus', onFocus);
    };
  }, [load]);

  return (
    <>
      <Head>
        <title>Analytics · EVF</title>
      </Head>

      <div className="min-h-screen bg-slate-100 pb-16 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
        <NavBar current="analytics" />

        <main className="mx-auto max-w-6xl space-y-4 p-3">
          <p className="text-sm text-slate-600 dark:text-slate-300">
            Figures for every site, whenever you want them: you do not need to finish reviewing first. They keep
            counting events you have downloaded and removed, and rows imported from a spreadsheet.
          </p>

          {error && (
            <p role="alert" className="rounded-lg bg-rose-100 px-3 py-2 text-sm text-rose-900 dark:bg-rose-900/40 dark:text-rose-200">
              Couldn&apos;t load the analytics: {error}{' '}
              <button type="button" className="font-semibold underline" onClick={load}>Retry</button>
            </p>
          )}

          {sites === null && !error && <p className="text-sm text-slate-500">Loading…</p>}

          {sites?.length === 0 && (
            <p className="rounded-xl border border-dashed border-slate-300 p-8 text-center text-sm text-slate-500 dark:border-slate-600 dark:text-slate-400">
              No sites yet. <Link href={ROUTES.import} className="font-medium text-indigo-600 underline dark:text-indigo-400">Import some data</Link> to create one.
            </p>
          )}

          {sites?.map((site) => <SiteCard key={site.site_id} site={site} onChanged={load} />)}
        </main>
      </div>
    </>
  );
}

export default function AnalyticsPage() {
  return (
    <RequireAuth>
      <AnalyticsPageInner />
    </RequireAuth>
  );
}
