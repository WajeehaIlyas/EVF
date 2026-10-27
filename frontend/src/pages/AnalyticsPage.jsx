import Head from 'next/head';
import Link from 'next/link';
import { useCallback, useEffect, useMemo, useState } from 'react';

import MetricCard, { TONE_STYLES, formatPercent, toneFor } from '../components/analytics/MetricCard';
import MmcAccuracyTable from '../components/analytics/MmcAccuracyTable';
import {
  downloadCsv,
  exportToGoogleSheets,
  getErrorMessage,
  getExportStatus,
  getMetricsSummary,
  getSites,
  isRequestCancelled,
} from '../services/api';
import { FEATURES } from '../store/reviewStore';

const PREFS_KEY = 'aevaf.analyticsPrefs';
const THEME_KEY = 'aevaf.theme';
const REFRESH_MS = 30000;
const FEATURE_LABEL = Object.fromEntries(FEATURES.map((f) => [f.value, f.label]));

const fieldClass =
  'rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-sm text-slate-900 shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-300 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100';

function loadPrefs() {
  try {
    return JSON.parse(window.localStorage.getItem(PREFS_KEY) || '{}');
  } catch {
    return {};
  }
}

function Spinner({ className = 'h-5 w-5' }) {
  return (
    <span
      role="status"
      className={`inline-block animate-spin rounded-full border-2 border-slate-300 border-t-indigo-500 dark:border-slate-600 ${className}`}
    >
      <span className="sr-only">Loading…</span>
    </span>
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

// ------------------------------------------------------------------ by-feature table
function ByFeatureTable({ rows }) {
  const pct = (value) => (
    <span className={`font-semibold tabular-nums ${TONE_STYLES[toneFor(value)].text}`}>{formatPercent(value)}</span>
  );
  return (
    <section className="rounded-xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <h3 className="px-4 pt-4 text-sm font-semibold text-slate-700 dark:text-slate-200">By feature</h3>
      <div className="overflow-x-auto p-4">
        <table className="w-full min-w-[640px] text-left text-sm">
          <thead>
            <tr className="border-b border-slate-200 text-[11px] uppercase tracking-wide text-slate-500 dark:border-slate-700 dark:text-slate-400">
              {['Feature', 'Reviewed', 'TP', 'FP', 'Unsure', 'Precision', 'VRM', 'Make', 'Model', 'Color'].map((h, i) => (
                <th key={h} className={`px-3 py-2 font-medium ${i === 0 ? 'pl-0' : 'text-right'}`}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map(({ feature, metrics: m }) => (
              <tr key={feature} className="border-b border-slate-100 last:border-0 dark:border-slate-800">
                <td className="py-2 pr-3 font-medium text-slate-900 dark:text-slate-100">{FEATURE_LABEL[feature] || feature}</td>
                <td className="px-3 py-2 text-right tabular-nums">{m.total_reviewed.toLocaleString()}</td>
                <td className="px-3 py-2 text-right tabular-nums text-emerald-700 dark:text-emerald-400">{m.tp}</td>
                <td className="px-3 py-2 text-right tabular-nums text-rose-700 dark:text-rose-400">{m.fp}</td>
                <td className="px-3 py-2 text-right tabular-nums text-amber-700 dark:text-amber-400">{m.unsure}</td>
                <td className="px-3 py-2 text-right">{pct(m.precision_pct)}</td>
                <td className="px-3 py-2 text-right">{pct(m.accuracy.vrm?.accuracy_pct ?? null)}</td>
                <td className="px-3 py-2 text-right">{pct(m.accuracy.make?.accuracy_pct ?? null)}</td>
                <td className="px-3 py-2 text-right">{pct(m.accuracy.model?.accuracy_pct ?? null)}</td>
                <td className="px-3 py-2 text-right">{pct(m.accuracy.color?.accuracy_pct ?? null)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------------------- page
export default function AnalyticsPage() {
  const [sites, setSites] = useState([]);
  const [sitesStatus, setSitesStatus] = useState('loading'); // loading | ready | error
  const [sitesError, setSitesError] = useState(null);

  const [siteId, setSiteId] = useState(null);
  const [feature, setFeature] = useState(''); // '' = all features
  const [fnMap, setFnMap] = useState({}); // "siteId:feature" -> known missed detections
  const [fnInput, setFnInput] = useState('');
  const [layout, setLayout] = useState('9-col');
  const [spreadsheetInput, setSpreadsheetInput] = useState('');
  const [worksheetName, setWorksheetName] = useState('');
  const [prefsLoaded, setPrefsLoaded] = useState(false);
  const [theme, setTheme] = useState(null);

  const [summary, setSummary] = useState(null); // { key, data }
  const [summaryError, setSummaryError] = useState(null);
  const [isFetching, setIsFetching] = useState(false);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [refreshTick, setRefreshTick] = useState(0);

  const [exportStatus, setExportStatus] = useState(null);
  const [sheets, setSheets] = useState({ status: 'idle' }); // idle | working | done | error
  const [csv, setCsv] = useState({ status: 'idle' });

  const contextKey = siteId ? `${siteId}|${feature}` : null;
  const fnKey = siteId ? `${siteId}:${feature || 'all'}` : null;
  const falseNegatives = fnKey && fnMap[fnKey] !== undefined ? fnMap[fnKey] : null;

  // ---- preferences + theme -------------------------------------------------------------
  useEffect(() => {
    const prefs = loadPrefs();
    if (prefs.siteId) setSiteId(Number(prefs.siteId));
    if (typeof prefs.feature === 'string') setFeature(prefs.feature);
    if (prefs.layout === '9-col' || prefs.layout === '16-col') setLayout(prefs.layout);
    if (typeof prefs.spreadsheetInput === 'string') setSpreadsheetInput(prefs.spreadsheetInput);
    if (typeof prefs.worksheetName === 'string') setWorksheetName(prefs.worksheetName);
    if (prefs.fnMap && typeof prefs.fnMap === 'object') setFnMap(prefs.fnMap);
    if (typeof prefs.autoRefresh === 'boolean') setAutoRefresh(prefs.autoRefresh);
    setPrefsLoaded(true);
  }, []);

  useEffect(() => {
    if (!prefsLoaded) return;
    try {
      window.localStorage.setItem(PREFS_KEY, JSON.stringify({
        siteId, feature, layout, spreadsheetInput, worksheetName, fnMap, autoRefresh,
      }));
    } catch { /* storage unavailable */ }
  }, [prefsLoaded, siteId, feature, layout, spreadsheetInput, worksheetName, fnMap, autoRefresh]);

  useEffect(() => {
    let saved = null;
    try { saved = window.localStorage.getItem(THEME_KEY); } catch { /* ignore */ }
    const prefersDark = window.matchMedia?.('(prefers-color-scheme: dark)').matches;
    setTheme(saved || (prefersDark ? 'dark' : 'light'));
  }, []);

  useEffect(() => {
    if (!theme) return;
    document.documentElement.classList.toggle('dark', theme === 'dark');
    try { window.localStorage.setItem(THEME_KEY, theme); } catch { /* ignore */ }
  }, [theme]);

  // ---- sites + export status --------------------------------------------------------------
  const loadSites = useCallback(async () => {
    setSitesStatus('loading');
    setSitesError(null);
    try {
      setSites(await getSites());
      setSitesStatus('ready');
    } catch (err) {
      setSitesError(getErrorMessage(err));
      setSitesStatus('error');
    }
  }, []);

  useEffect(() => { loadSites(); }, [loadSites]);

  useEffect(() => {
    if (sitesStatus === 'ready' && siteId && !sites.some((s) => s.id === siteId)) setSiteId(null);
  }, [sitesStatus, sites, siteId]);

  useEffect(() => {
    getExportStatus()
      .then(setExportStatus)
      .catch((err) => setExportStatus({
        google_sheets_configured: false, service_account_email: null, detail: getErrorMessage(err),
      }));
  }, []);

  // The known-missed-detections box follows the selected site/feature.
  useEffect(() => {
    setFnInput(fnKey && fnMap[fnKey] !== undefined ? String(fnMap[fnKey]) : '');
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only re-sync when the selection changes
  }, [fnKey]);

  const commitFalseNegatives = () => {
    if (!fnKey) return;
    const trimmed = fnInput.trim();
    const parsed = Number.parseInt(trimmed, 10);
    const valid = trimmed !== '' && !Number.isNaN(parsed) && parsed >= 0;
    setFnInput(valid ? String(parsed) : '');
    setFnMap((prev) => {
      const next = { ...prev };
      if (valid) next[fnKey] = parsed;
      else delete next[fnKey];
      return next;
    });
  };

  // ---- metrics (live) -------------------------------------------------------------------
  useEffect(() => {
    if (!siteId) {
      setSummary(null);
      return undefined;
    }
    const controller = new AbortController();
    let cancelled = false;
    setIsFetching(true);

    getMetricsSummary(siteId, feature || null, { falseNegatives, signal: controller.signal })
      .then((data) => {
        if (cancelled) return;
        setSummary({ key: contextKey, data });
        setSummaryError(null);
        setLastUpdated(new Date());
      })
      .catch((err) => {
        if (cancelled || isRequestCancelled(err)) return;
        setSummaryError(getErrorMessage(err));
      })
      .finally(() => {
        if (!cancelled) setIsFetching(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [siteId, feature, falseNegatives, refreshTick, contextKey]);

  useEffect(() => {
    if (!autoRefresh || !siteId) return undefined;
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') setRefreshTick((n) => n + 1);
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [autoRefresh, siteId]);

  // Another tab finished an import (or this tab regained focus): recalculate straight away.
  useEffect(() => {
    const refresh = () => setRefreshTick((n) => n + 1);
    const onStorage = (event) => { if (event.key === 'aevaf.dataVersion') refresh(); };
    window.addEventListener('storage', onStorage);
    window.addEventListener('focus', refresh);
    return () => {
      window.removeEventListener('storage', onStorage);
      window.removeEventListener('focus', refresh);
    };
  }, []);

  const data = summary && summary.key === contextKey ? summary.data : null;
  const canExport = Boolean(siteId && data && data.total_reviewed > 0);
  const sheetsConfigured = exportStatus?.google_sheets_configured === true;
  const scopeLabel = feature ? FEATURE_LABEL[feature] : 'All features';
  const siteLabel = useMemo(() => sites.find((s) => s.id === siteId)?.name, [sites, siteId]);

  // ---- exports -----------------------------------------------------------------------------
  const handleSheetsExport = async () => {
    setSheets({ status: 'working' });
    try {
      const result = await exportToGoogleSheets({
        spreadsheet_id: spreadsheetInput.trim(),
        site_id: siteId,
        feature: feature || null,
        layout,
        worksheet_name: worksheetName.trim() || null,
      });
      setSheets({ status: 'done', result });
    } catch (err) {
      setSheets({ status: 'error', error: getErrorMessage(err) });
    }
  };

  const handleCsvDownload = async () => {
    setCsv({ status: 'working' });
    try {
      const filename = await downloadCsv({ siteId, feature: feature || null, layout });
      setCsv({ status: 'done', filename });
    } catch (err) {
      setCsv({ status: 'error', error: getErrorMessage(err) });
    }
  };

  // ---- render ----------------------------------------------------------------------------------
  let content;
  if (!siteId) {
    content = (
      <div className="mx-auto mt-16 max-w-md rounded-xl border border-slate-200 bg-white p-8 text-center text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300">
        {sitesStatus === 'loading' && <Spinner />}
        {sitesStatus === 'error' && <p className="text-rose-600 dark:text-rose-400">{sitesError}</p>}
        {sitesStatus === 'ready' && (sites.length === 0
          ? 'No sites exist yet. Create one and review some events first.'
          : 'Choose a site to see its accuracy metrics.')}
      </div>
    );
  } else if (!data && summaryError) {
    content = (
      <div className="mx-auto mt-16 max-w-md rounded-xl border border-rose-300 bg-white p-8 text-center shadow-sm dark:border-rose-800 dark:bg-slate-900">
        <h2 className="font-semibold">Couldn&apos;t load metrics</h2>
        <p className="mt-2 text-sm text-rose-600 dark:text-rose-400">{summaryError}</p>
        <div className="mt-4"><Button onClick={() => setRefreshTick((n) => n + 1)}>Try again</Button></div>
      </div>
    );
  } else if (!data) {
    content = (
      <div className="mt-24 flex flex-col items-center gap-3 text-sm text-slate-600 dark:text-slate-300">
        <Spinner className="h-9 w-9" />
        Calculating metrics…
      </div>
    );
  } else if (data.total_reviewed === 0) {
    content = (
      <div className="mx-auto mt-16 max-w-md rounded-xl border border-slate-200 bg-white p-8 text-center shadow-sm dark:border-slate-700 dark:bg-slate-900">
        <h2 className="font-semibold">Nothing reviewed yet</h2>
        <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
          {data.total_events > 0 ? `${data.total_events.toLocaleString()} events are imported for ${scopeLabel} at this site, but none have been reviewed yet.` : `${scopeLabel} at this site has no imported events yet.`} Metrics appear as soon as reviews are saved.
        </p>
        <div className="mt-4">
          <Link href="/" className="text-sm font-semibold text-indigo-600 hover:underline dark:text-indigo-400">Go to the review workspace →</Link>
        </div>
      </div>
    );
  } else {
    const { day, night } = data.by_day_night;
    content = (
      <div className="space-y-4">
        {summaryError && (
          <p className="rounded-lg bg-amber-100 px-3 py-2 text-xs text-amber-900 dark:bg-amber-900/40 dark:text-amber-200">
            Couldn&apos;t refresh ({summaryError}). Showing the last values received.
          </p>
        )}

        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <MetricCard
            title="Raw Precision"
            subtitle="TP ÷ (TP + FP)"
            value={data.precision_pct}
            detail={`${data.tp} TP · ${data.fp} FP${data.unsure ? ` · ${data.unsure} unsure (excluded)` : ''}`}
            day={day.precision_pct}
            night={night.precision_pct}
            emptyText="Needs at least one TP or FP"
          />

          <MetricCard
            title="Recall"
            subtitle="TP ÷ (TP + FN)"
            value={data.recall_pct}
            detail={data.recall_pct === null ? data.recall_note : `Based on ${data.false_negatives} known missed events`}
            emptyText="Not available"
          >
            <label className="block text-xs text-slate-600 dark:text-slate-300">
              Known missed events (FN)
              <input
                type="number"
                min="0"
                inputMode="numeric"
                value={fnInput}
                onChange={(e) => setFnInput(e.target.value)}
                onBlur={commitFalseNegatives}
                onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); }}
                placeholder="e.g. 12"
                className={`${fieldClass} mt-1 block w-full`}
              />
            </label>
            <p className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
              Reviews only cover detections, so missed events must be entered here. Saved per site and feature.
            </p>
          </MetricCard>

          <MetricCard
            title="MMC Accuracy"
            subtitle="Make + model + color, ticks ÷ checked"
            value={data.accuracy.mmc?.accuracy_pct ?? null}
            detail={`${data.accuracy.mmc?.ticks ?? 0} ✓ · ${data.accuracy.mmc?.crosses ?? 0} ✕`}
            day={day.accuracy.mmc?.accuracy_pct ?? null}
            night={night.accuracy.mmc?.accuracy_pct ?? null}
            emptyText="No MMC checks recorded"
          />

          <MetricCard
            title="Reviewed"
            subtitle={`${scopeLabel}${siteLabel ? ` · ${siteLabel}` : ''}`}
            format="count"
            value={data.total_reviewed}
            detail={`${data.tp} TP · ${data.fp} FP · ${data.unsure} unsure · ${data.pending_review.toLocaleString()} of ${data.total_events.toLocaleString()} imported events still pending review`}
            day={day.total_reviewed}
            night={night.total_reviewed}
          >
            <Link
              href={`/import?site=${siteId}${feature ? `&feature=${feature}` : ''}`}
              className="text-xs font-semibold text-indigo-600 hover:underline dark:text-indigo-400"
            >
              + Add more data to this site
            </Link>
            {data.last_ingested_at && (
              <p className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
                Last import: {new Date(data.last_ingested_at.endsWith('Z') ? data.last_ingested_at : `${data.last_ingested_at}Z`).toLocaleString()}
              </p>
            )}
          </MetricCard>
        </div>

        <MmcAccuracyTable
          overall={data.accuracy}
          day={day.accuracy}
          night={night.accuracy}
        />

        {!feature && data.by_feature.length > 1 && <ByFeatureTable rows={data.by_feature} />}

        {/* -------------------------------------------------------------- exports */}
        <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900">
          <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-200">Export reviewed events</h3>
          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            Exports the {data.total_reviewed.toLocaleString()} reviewed events for {scopeLabel}
            {siteLabel ? ` at ${siteLabel}` : ''}.
          </p>

          <fieldset className="mt-3 flex flex-wrap gap-4 text-sm">
            <legend className="sr-only">Layout</legend>
            {[
              { value: '9-col', label: '9 columns', hint: 'Plate, time, label, issue, OCR, clarity' },
              { value: '16-col', label: '16 columns', hint: 'adds brand / color / model, scores, MMC status' },
            ].map((option) => (
              <label key={option.value} className="flex cursor-pointer items-start gap-2">
                <input
                  type="radio"
                  name="layout"
                  value={option.value}
                  checked={layout === option.value}
                  onChange={() => setLayout(option.value)}
                  className="mt-1"
                />
                <span>
                  <span className="font-medium text-slate-900 dark:text-slate-100">{option.label}</span>
                  <span className="block text-[11px] text-slate-500 dark:text-slate-400">{option.hint}</span>
                </span>
              </label>
            ))}
          </fieldset>

          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            {/* Google Sheets */}
            <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-700">
              <h4 className="text-sm font-semibold text-slate-800 dark:text-slate-100">Google Sheets</h4>
              {exportStatus && !sheetsConfigured && (
                <p className="mt-2 rounded bg-amber-100 px-2.5 py-2 text-xs text-amber-900 dark:bg-amber-900/40 dark:text-amber-200">
                  {exportStatus.detail}
                </p>
              )}
              {sheetsConfigured && exportStatus.service_account_email && (
                <p className="mt-2 text-xs text-slate-600 dark:text-slate-300">
                  Share the spreadsheet (Editor) with{' '}
                  <code className="break-all rounded bg-slate-100 px-1 dark:bg-slate-800">{exportStatus.service_account_email}</code>
                </p>
              )}

              <label className="mt-3 block text-xs text-slate-600 dark:text-slate-300">
                Spreadsheet ID or URL
                <input
                  type="text"
                  value={spreadsheetInput}
                  onChange={(e) => setSpreadsheetInput(e.target.value)}
                  placeholder="https://docs.google.com/spreadsheets/d/…"
                  className={`${fieldClass} mt-1 block w-full`}
                />
              </label>
              <label className="mt-2 block text-xs text-slate-600 dark:text-slate-300">
                Worksheet name <span className="text-slate-400">(optional; replaced on every export)</span>
                <input
                  type="text"
                  value={worksheetName}
                  maxLength={100}
                  onChange={(e) => setWorksheetName(e.target.value)}
                  placeholder="Auto: site, feature and layout"
                  className={`${fieldClass} mt-1 block w-full`}
                />
              </label>

              <div className="mt-3">
                <Button
                  onClick={handleSheetsExport}
                  busy={sheets.status === 'working'}
                  disabled={!canExport || !sheetsConfigured || !spreadsheetInput.trim() || sheets.status === 'working'}
                >
                  {sheets.status === 'working' ? 'Exporting…' : 'Export to Google Sheets'}
                </Button>
              </div>

              {sheets.status === 'done' && (
                <p className="mt-3 rounded bg-emerald-100 px-2.5 py-2 text-xs text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200" role="status">
                  Wrote {sheets.result.rows_written.toLocaleString()} rows to “{sheets.result.worksheet}”.{' '}
                  <a href={sheets.result.spreadsheet_url} target="_blank" rel="noopener noreferrer" className="font-semibold underline">Open sheet</a>
                </p>
              )}
              {sheets.status === 'error' && (
                <p className="mt-3 rounded bg-rose-100 px-2.5 py-2 text-xs text-rose-900 dark:bg-rose-900/40 dark:text-rose-200" role="alert">
                  {sheets.error}
                </p>
              )}
            </div>

            {/* CSV */}
            <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-700">
              <h4 className="text-sm font-semibold text-slate-800 dark:text-slate-100">CSV file</h4>
              <p className="mt-2 text-xs text-slate-600 dark:text-slate-300">
                Downloads the same table as a UTF-8 CSV that opens cleanly in Excel. Works without any Google setup.
              </p>
              <div className="mt-3">
                <Button
                  variant="secondary"
                  onClick={handleCsvDownload}
                  busy={csv.status === 'working'}
                  disabled={!canExport || csv.status === 'working'}
                >
                  {csv.status === 'working' ? 'Preparing…' : 'Download CSV'}
                </Button>
              </div>
              {csv.status === 'done' && (
                <p className="mt-3 text-xs text-emerald-700 dark:text-emerald-400" role="status">Downloaded {csv.filename}</p>
              )}
              {csv.status === 'error' && (
                <p className="mt-3 rounded bg-rose-100 px-2.5 py-2 text-xs text-rose-900 dark:bg-rose-900/40 dark:text-rose-200" role="alert">
                  {csv.error}
                </p>
              )}
            </div>
          </div>
        </section>
      </div>
    );
  }

  return (
    <>
      <Head>
        <title>Analytics · AEVAF</title>
      </Head>

      <div className="min-h-screen bg-slate-100 pb-10 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
        <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/95 backdrop-blur dark:border-slate-700 dark:bg-slate-900/95">
          <div className="mx-auto flex max-w-screen-2xl flex-wrap items-center gap-x-4 gap-y-2 px-3 py-2.5">
            <h1 className="text-base font-bold tracking-tight">
              AEVAF <span className="font-normal text-slate-500 dark:text-slate-400">· Analytics</span>
            </h1>
            <Link href="/" className="text-xs font-medium text-indigo-600 hover:underline dark:text-indigo-400">
              ← Review workspace
            </Link>
            <Link href="/import" className="text-xs font-medium text-indigo-600 hover:underline dark:text-indigo-400">
              Import data
            </Link>

            <label className="flex items-center gap-2 text-xs text-slate-600 dark:text-slate-300">
              Site
              <select
                className={fieldClass}
                value={siteId ?? ''}
                disabled={sitesStatus !== 'ready'}
                onChange={(e) => setSiteId(e.target.value ? Number(e.target.value) : null)}
              >
                <option value="">{sitesStatus === 'loading' ? 'Loading…' : 'Select site…'}</option>
                {sites.map((site) => (
                  <option key={site.id} value={site.id}>{site.name} ({site.code})</option>
                ))}
              </select>
            </label>

            <label className="flex items-center gap-2 text-xs text-slate-600 dark:text-slate-300">
              Feature
              <select className={fieldClass} value={feature} onChange={(e) => setFeature(e.target.value)}>
                <option value="">All features</option>
                {FEATURES.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
              </select>
            </label>

            {sitesStatus === 'error' && (
              <button type="button" onClick={loadSites} className="text-xs font-medium text-rose-600 underline dark:text-rose-400" title={sitesError}>
                Couldn&apos;t load sites — retry
              </button>
            )}

            <div className="ml-auto flex items-center gap-3 text-xs text-slate-600 dark:text-slate-300">
              {isFetching && <Spinner className="h-4 w-4" />}
              {lastUpdated && <span>Updated {lastUpdated.toLocaleTimeString()}</span>}
              <label className="flex cursor-pointer items-center gap-1.5">
                <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
                Live (30 s)
              </label>
              <Button variant="secondary" onClick={() => setRefreshTick((n) => n + 1)} disabled={!siteId || isFetching}>
                Refresh
              </Button>
              <button
                type="button"
                onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
                aria-label="Toggle dark mode"
                className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-sm dark:border-slate-600"
              >
                {theme === 'dark' ? '☀' : '☾'}
              </button>
            </div>
          </div>
        </header>

        <main className="mx-auto max-w-screen-2xl p-3">{content}</main>
      </div>
    </>
  );
}
