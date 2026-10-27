import Head from 'next/head';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import DeleteSiteDialog from '../components/DeleteSiteDialog';
import EvidenceViewer from '../components/EvidenceViewer';
import NavBar from '../components/NavBar';
import { ROUTES } from '../lib/routes';
import { browseEvents, getAnalytics, getErrorMessage, siteXlsxUrl } from '../services/api';
import { FEATURES } from '../store/reviewStore';
import RequireAuth from '../components/RequireAuth';

const DATA_VERSION_KEY = 'evf.dataVersion';
const FEATURE_LABEL = Object.fromEntries(FEATURES.map((f) => [f.value, f.label]));
const labelOf = (feature) => FEATURE_LABEL[feature] || feature;
const PAGE_SIZES = [10, 25, 50, 100];
const PERIODS = [
  ['all', 'All time'], ['today', 'Today'], ['yesterday', 'Yesterday'],
  ['last7', 'Last 7 days'], ['last30', 'Last 30 days'], ['month', 'This month'],
];
const EMPTY_FILTERS = { mode: 'period', period: 'all', from: '', to: '', plate: '', feature: '', dayNight: '', review: '' };

const fieldClass =
  'rounded border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-300 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100';

// -------------------------------------------------------------------------------- helpers
const pad = (n) => String(n).padStart(2, '0');
/** Naive local ISO ('2026-10-22T15:11:49'): events are stored as camera wall-clock time, no zone. */
const toIso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;

function periodRange(period) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const day = 86400000;
  switch (period) {
    case 'today': return [today, null];
    case 'yesterday': return [new Date(today - day), new Date(today - 1)];
    case 'last7': return [new Date(today - 6 * day), null];
    case 'last30': return [new Date(today - 29 * day), null];
    case 'month': return [new Date(now.getFullYear(), now.getMonth(), 1), null];
    default: return [null, null];
  }
}

/** The applied filters as API parameters. */
function dateParams(filters) {
  if (filters.mode === 'date') {
    return {
      dateFrom: filters.from ? `${filters.from}:00` : undefined,
      dateTo: filters.to ? `${filters.to}:59` : undefined,
    };
  }
  const [from, to] = periodRange(filters.period);
  return { dateFrom: from ? toIso(from) : undefined, dateTo: to ? toIso(to) : undefined };
}

function formatTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString('en-US', {
    month: '2-digit', day: '2-digit', year: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit',
  }).replace(',', '');
}

/** 1 2 3 4 5 … 44, like the table in your screenshot; the window follows the current page. */
function pageList(current, last) {
  if (last <= 7) return Array.from({ length: last }, (_, i) => i + 1);
  if (current <= 4) return [1, 2, 3, 4, 5, '…', last];
  if (current >= last - 3) return [1, '…', last - 4, last - 3, last - 2, last - 1, last];
  return [1, '…', current - 1, current, current + 1, '…', last];
}

const REVIEW_BADGE = {
  TP: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300',
  FP: 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300',
  Unsure: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
};

function EyeIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8S1 12 1 12z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

// ------------------------------------------------------------------------------------- page
function SitesPageInner() {
  const router = useRouter();

  // sites
  const [sites, setSites] = useState(null);
  const [sitesError, setSitesError] = useState(null);
  const [selected, setSelected] = useState('all'); // 'all' or a site id (number)
  const [deleting, setDeleting] = useState(null);
  const [notice, setNotice] = useState(null);

  // table
  const [draft, setDraft] = useState(EMPTY_FILTERS);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [quick, setQuick] = useState('');
  const [quickApplied, setQuickApplied] = useState('');
  const [pageSize, setPageSize] = useState(10);
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState({ key: 'time', order: 'desc' });
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [viewerAt, setViewerAt] = useState(null);
  const [version, setVersion] = useState(0); // bump to reload

  const mounted = useRef(true);
  const reload = useCallback(() => setVersion((v) => v + 1), []);

  // ---- sites list -----------------------------------------------------------------------------
  const loadSites = useCallback(async () => {
    try {
      const result = await getAnalytics();
      if (!mounted.current) return;
      setSites(result);
      setSitesError(null);
    } catch (err) {
      if (mounted.current) setSitesError(getErrorMessage(err));
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    loadSites();
    const refresh = () => { loadSites(); reload(); };
    const onStorage = (event) => { if (event.key === DATA_VERSION_KEY) refresh(); };
    window.addEventListener('storage', onStorage);
    window.addEventListener('focus', refresh);
    return () => {
      mounted.current = false;
      window.removeEventListener('storage', onStorage);
      window.removeEventListener('focus', refresh);
    };
  }, [loadSites, reload]);

  // ?site=ID selects a site (also written back when the user picks one)
  useEffect(() => {
    if (!router?.isReady) return;
    const fromUrl = router.query.site ? Number(router.query.site) : 'all';
    setSelected(Number.isFinite(fromUrl) ? fromUrl : 'all');
  }, [router?.isReady, router?.query?.site]); // eslint-disable-line react-hooks/exhaustive-deps

  const choose = (id) => {
    setSelected(id);
    setPage(1);
    setDraft((d) => ({ ...d, feature: '' }));
    setFilters((f) => ({ ...f, feature: '' }));
    router?.replace({ pathname: router.pathname, query: id === 'all' ? {} : { site: id } }, undefined, { shallow: true });
  };

  const selectedSite = useMemo(
    () => (selected === 'all' ? null : sites?.find((s) => s.site_id === selected) || null),
    [selected, sites],
  );

  // a site that was deleted elsewhere: fall back to all sites
  useEffect(() => {
    if (sites && selected !== 'all' && !sites.some((s) => s.site_id === selected)) setSelected('all');
  }, [sites, selected]);

  // ---- debounce the "Filter search" box ------------------------------------------------------------
  useEffect(() => {
    const timer = setTimeout(() => { setQuickApplied(quick.trim()); setPage(1); }, 300);
    return () => clearTimeout(timer);
  }, [quick]);

  // ---- load events ------------------------------------------------------------------------------------
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    browseEvents({
      siteId: selected === 'all' ? undefined : selected,
      feature: filters.feature,
      plate: filters.plate.trim(),
      search: quickApplied,
      dayNight: filters.dayNight,
      review: filters.review,
      ...dateParams(filters),
      sort: sort.key,
      order: sort.order,
      limit: pageSize,
      offset: (page - 1) * pageSize,
    }, { signal: controller.signal })
      .then((result) => {
        if (!mounted.current) return;
        setData(result);
        setError(null);
        setLoading(false);
        const last = Math.max(1, Math.ceil(result.total / pageSize));
        if (page > last) setPage(last);
      })
      .catch((err) => {
        if (err?.code === 'ERR_CANCELED' || controller.signal.aborted) return;
        if (!mounted.current) return;
        setError(getErrorMessage(err));
        setLoading(false);
      });
    return () => controller.abort();
  }, [selected, filters, quickApplied, sort, pageSize, page, version]);

  // ---- actions ------------------------------------------------------------------------------------------
  const search = (event) => {
    event?.preventDefault();
    setFilters(draft);
    setPage(1);
  };
  const clearFilters = () => {
    setDraft(EMPTY_FILTERS);
    setFilters(EMPTY_FILTERS);
    setQuick('');
    setQuickApplied('');
    setPage(1);
  };
  const sortBy = (key) => {
    setSort((current) => (current.key === key ? { key, order: current.order === 'asc' ? 'desc' : 'asc' } : { key, order: key === 'time' ? 'desc' : 'asc' }));
    setPage(1);
  };

  const items = data?.items || [];
  const total = data?.total ?? 0;
  const lastPage = Math.max(1, Math.ceil(total / pageSize));
  const from = total ? (page - 1) * pageSize + 1 : 0;
  const to = Math.min(total, page * pageSize);
  const showSite = selected === 'all';
  const featureOptions = data?.features || [];

  const exportRows = () => items.map((row, i) => ({
    'S.No.': from + i,
    Time: formatTime(row.offence_timestamp),
    'Violation Type': labelOf(row.feature),
    'Plate Number': row.vrm || '',
    'Day/Night': row.day_night,
    Make: row.vehicle_make || '',
    Model: row.vehicle_model || '',
    Color: row.vehicle_color || '',
    Review: row.review ? row.review.human_label : 'Not reviewed',
    ...(showSite ? { Site: row.site_name } : {}),
  }));

  const copyRows = async () => {
    const rows = exportRows();
    if (!rows.length) return;
    const text = [Object.keys(rows[0]).join('\t'), ...rows.map((r) => Object.values(r).join('\t'))].join('\n');
    try {
      await navigator.clipboard.writeText(text);
      setNotice(`Copied ${rows.length} rows.`);
    } catch {
      setNotice('Your browser would not allow copying.');
    }
  };
  const downloadCsv = () => {
    const rows = exportRows();
    if (!rows.length) return;
    const cell = (v) => `"${String(v).replace(/"/g, '""')}"`;
    const csv = [Object.keys(rows[0]).map(cell).join(','), ...rows.map((r) => Object.values(r).map(cell).join(','))].join('\r\n');
    const url = URL.createObjectURL(new Blob([`﻿${csv}`], { type: 'text/csv;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `violations_${selectedSite ? selectedSite.site_code.toLowerCase() : 'all-sites'}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  };
  const printRows = () => {
    const rows = exportRows();
    if (!rows.length) return;
    const esc = (v) => String(v).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
    const win = window.open('', '_blank');
    if (!win) return;
    win.document.write(`<title>Violation records</title><style>body{font:12px Arial}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:4px 6px;text-align:left}</style><table><thead><tr>${Object.keys(rows[0]).map((k) => `<th>${esc(k)}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${Object.values(r).map((v) => `<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
    win.document.close();
    win.focus();
    win.print();
  };

  /** Where the Review page should open for a site: the feature that has the most events waiting. */
  const reviewHref = (site) => {
    const best = [...site.features].sort((x, y) => y.stats.to_review - x.stats.to_review)[0];
    const query = new URLSearchParams({ site: String(site.site_id) });
    if (best && best.stats.to_review > 0) query.set('feature', best.feature);
    return `${ROUTES.review}${ROUTES.review.includes('?') ? '&' : '?'}${query}`;
  };

  const onDeleted = (result) => {
    setDeleting(null);
    setNotice(`Site deleted: ${result.events.toLocaleString()} events and ${result.files_removed.toLocaleString()} images removed.`);
    try { window.localStorage.setItem(DATA_VERSION_KEY, String(Date.now())); } catch { /* ignore */ }
    if (selected === result.site_id) choose('all');
    loadSites();
    reload();
  };

  const th = (key, label, extra = '') => (
    <th scope="col" className={`whitespace-nowrap px-3 py-3 text-left font-semibold ${extra}`} aria-sort={sort.key === key ? (sort.order === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button type="button" onClick={() => sortBy(key)} className="inline-flex items-center gap-1.5 hover:text-indigo-600 dark:hover:text-indigo-400">
        {label}
        <span className={`text-[10px] ${sort.key === key ? 'text-indigo-600 dark:text-indigo-400' : 'text-slate-400'}`} aria-hidden="true">
          {sort.key === key ? (sort.order === 'asc' ? '↑' : '↓') : '↑↓'}
        </span>
      </button>
    </th>
  );

  const toolButton = 'px-2.5 py-1 text-xs font-medium text-white bg-slate-400 hover:bg-slate-500 first:rounded-l last:rounded-r disabled:opacity-50 dark:bg-slate-600 dark:hover:bg-slate-500';
  const siteTotal = (s) => s.overall.total_violations;

  return (
    <>
      <Head>
        <title>Sites · EVF</title>
      </Head>

      <div className="min-h-screen bg-slate-100 pb-16 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
        <NavBar current="sites" />

        <main className="mx-auto max-w-[1500px] space-y-4 p-3">
          {notice && (
            <p role="status" className="flex items-start justify-between gap-3 rounded-lg bg-emerald-100 px-3 py-2 text-sm text-emerald-900 dark:bg-emerald-900/30 dark:text-emerald-200">
              {notice}
              <button type="button" onClick={() => setNotice(null)} aria-label="Dismiss">✕</button>
            </p>
          )}

          {/* ----------------------------------------------------------------------- sites */}
          <section aria-label="Sites">
            {sitesError && (
              <p role="alert" className="mb-2 rounded-lg bg-rose-100 px-3 py-2 text-sm text-rose-900 dark:bg-rose-900/40 dark:text-rose-200">
                Couldn&apos;t load your sites: {sitesError}{' '}
                <button type="button" className="font-semibold underline" onClick={loadSites}>Retry</button>
              </p>
            )}
            {sites === null && !sitesError && <p className="text-sm text-slate-500">Loading sites…</p>}
            {sites?.length === 0 && (
              <p className="rounded-xl border border-dashed border-slate-300 p-8 text-center text-sm text-slate-500 dark:border-slate-600 dark:text-slate-400">
                No sites yet. <Link href={ROUTES.import} className="font-medium text-indigo-600 underline dark:text-indigo-400">Import some data</Link> to create one.
              </p>
            )}
            {sites?.length > 0 && (
              <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                <li>
                  <button type="button" onClick={() => choose('all')} aria-pressed={selected === 'all'}
                    className={`h-full w-full rounded-xl border p-3 text-left transition ${selected === 'all' ? 'border-indigo-500 bg-indigo-50 ring-1 ring-indigo-500 dark:bg-indigo-950/40' : 'border-slate-200 bg-white hover:border-slate-300 dark:border-slate-700 dark:bg-slate-900'}`}>
                    <span className="block font-semibold">All sites</span>
                    <span className="text-xs text-slate-500 dark:text-slate-400">{sites.length} sites · {sites.reduce((n, s) => n + siteTotal(s), 0).toLocaleString()} violations</span>
                  </button>
                </li>
                {sites.map((site) => (
                  <li key={site.site_id} className={`rounded-xl border p-3 transition ${selected === site.site_id ? 'border-indigo-500 bg-indigo-50 ring-1 ring-indigo-500 dark:bg-indigo-950/40' : 'border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900'}`}>
                    <button type="button" onClick={() => choose(site.site_id)} aria-pressed={selected === site.site_id} className="block w-full text-left">
                      <span className="block truncate font-semibold">{site.site_name}</span>
                      <span className="text-xs text-slate-500 dark:text-slate-400">
                        {site.site_code} · {siteTotal(site).toLocaleString()} violations · {site.overall.reviewed.toLocaleString()} reviewed
                      </span>
                    </button>
                    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs font-medium">
                      {site.overall.to_review > 0 && (
                        <Link href={reviewHref(site)} className="rounded bg-indigo-600 px-2 py-0.5 text-white hover:bg-indigo-700" title="Open this site on the Review page">
                          Review {site.overall.to_review.toLocaleString()} unreviewed
                        </Link>
                      )}
                      <a href={siteXlsxUrl(site.site_id)} className="text-indigo-600 hover:underline dark:text-indigo-400" title="Download this site's events as an .xlsx workbook">Download .xlsx</a>
                      <Link href={ROUTES.analytics} className="text-indigo-600 hover:underline dark:text-indigo-400">Analytics</Link>
                      <button type="button" onClick={() => setDeleting(site)} className="ml-auto text-rose-600 hover:underline dark:text-rose-400">Delete</button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* ----------------------------------------------------------------------- records */}
          {sites?.length > 0 && (
            <section className="rounded-lg border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-900" aria-label="Violation records">
              <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3.5 dark:border-slate-700">
                <h2 className="text-base font-medium">
                  Search Violation Records
                  <span className="ml-2 text-sm font-normal text-slate-500 dark:text-slate-400">{selectedSite ? selectedSite.site_name : 'All sites'}</span>
                </h2>
                <button type="button" onClick={clearFilters} className="rounded border border-indigo-500 px-3 py-1.5 text-sm text-indigo-600 hover:bg-indigo-50 dark:text-indigo-400 dark:hover:bg-indigo-950/40">
                  Clear filters
                </button>
              </div>

              <form onSubmit={search} className="space-y-4 px-5 py-4">
                <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
                  <label className="flex items-center gap-2 text-sm">
                    <input type="radio" name="mode" checked={draft.mode === 'period'} onChange={() => setDraft({ ...draft, mode: 'period' })} />
                    By Period
                    <select value={draft.period} onChange={(e) => setDraft({ ...draft, mode: 'period', period: e.target.value })} className={fieldClass} aria-label="Period">
                      {PERIODS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                    </select>
                  </label>
                  <label className="flex items-center gap-2 text-sm">
                    <input type="radio" name="mode" checked={draft.mode === 'date'} onChange={() => setDraft({ ...draft, mode: 'date' })} />
                    By Date
                  </label>
                  <label className="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
                    From
                    <input type="datetime-local" value={draft.from} onChange={(e) => setDraft({ ...draft, mode: 'date', from: e.target.value })} className={fieldClass} />
                  </label>
                  <label className="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
                    To
                    <input type="datetime-local" value={draft.to} onChange={(e) => setDraft({ ...draft, mode: 'date', to: e.target.value })} className={fieldClass} />
                  </label>
                  <button type="submit" className="ml-auto rounded bg-indigo-600 px-8 py-2 text-sm font-medium text-white hover:bg-indigo-700">Search</button>
                </div>

                <div className="flex flex-wrap items-center gap-x-4 gap-y-3 text-sm text-slate-600 dark:text-slate-300">
                  <label className="flex items-center gap-2">
                    Plate Number
                    <input value={draft.plate} onChange={(e) => setDraft({ ...draft, plate: e.target.value })} placeholder="Plate Number" className={`${fieldClass} w-40`} />
                  </label>
                  <label className="flex items-center gap-2">
                    Violation Type
                    <select value={draft.feature} onChange={(e) => setDraft({ ...draft, feature: e.target.value })} className={`${fieldClass} min-w-36`}>
                      <option value="">All</option>
                      {featureOptions.map((f) => <option key={f} value={f}>{labelOf(f)}</option>)}
                    </select>
                  </label>
                  <label className="flex items-center gap-2">
                    Day / Night
                    <select value={draft.dayNight} onChange={(e) => setDraft({ ...draft, dayNight: e.target.value })} className={`${fieldClass} min-w-28`}>
                      <option value="">All</option>
                      <option value="Day">Day</option>
                      <option value="Night">Night</option>
                    </select>
                  </label>
                  <label className="flex items-center gap-2">
                    Review
                    <select value={draft.review} onChange={(e) => setDraft({ ...draft, review: e.target.value })} className={`${fieldClass} min-w-36`}>
                      <option value="">All</option>
                      <option value="unreviewed">Not reviewed</option>
                      <option value="reviewed">Reviewed</option>
                      <option value="TP">True positive</option>
                      <option value="FP">False positive</option>
                      <option value="Unsure">Unsure</option>
                    </select>
                  </label>
                </div>
              </form>

              <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 pb-3 text-sm">
                <label className="flex items-center gap-2">
                  Show
                  <select value={pageSize} onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1); }} className={fieldClass}>
                    {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
                  </select>
                  entries
                </label>
                <p className="mx-auto text-slate-600 dark:text-slate-300" aria-live="polite">
                  {loading && !data ? 'Loading…' : `Loaded ${total.toLocaleString()} out of ${(data?.site_total ?? 0).toLocaleString()}`}
                </p>
                <label className="flex items-center gap-2">
                  Filter search:
                  <input value={quick} onChange={(e) => setQuick(e.target.value)} className={`${fieldClass} w-44`} />
                </label>
                <div className="inline-flex" role="group" aria-label="Export this page">
                  <button type="button" className={toolButton} onClick={copyRows} disabled={!items.length}>Copy</button>
                  <button type="button" className={toolButton} onClick={downloadCsv} disabled={!items.length}>CSV</button>
                  {selectedSite
                    ? <a className={`${toolButton} inline-block`} href={siteXlsxUrl(selectedSite.site_id, filters.feature)} title="Download the whole site as an .xlsx workbook">Excel</a>
                    : <button type="button" className={toolButton} disabled title="Pick a site to download its workbook">Excel</button>}
                  <button type="button" className={toolButton} onClick={printRows} disabled={!items.length}>Print</button>
                </div>
              </div>

              {error && (
                <p role="alert" className="mx-5 mb-3 rounded-lg bg-rose-100 px-3 py-2 text-sm text-rose-900 dark:bg-rose-900/40 dark:text-rose-200">
                  Couldn&apos;t load the records: {error}{' '}
                  <button type="button" className="font-semibold underline" onClick={reload}>Retry</button>
                </p>
              )}

              <div className={`overflow-x-auto px-5 transition-opacity ${loading ? 'opacity-60' : ''}`}>
                <table className="w-full min-w-[900px] text-sm">
                  <thead className="bg-slate-100 text-slate-800 dark:bg-slate-800 dark:text-slate-100">
                    <tr>
                      <th scope="col" className="px-3 py-3 text-left font-semibold">S.No.</th>
                      {th('time', 'Time')}
                      {th('type', 'Violation Type')}
                      {th('plate', 'Plate Number')}
                      {th('day_night', 'Day / Night')}
                      {th('make', 'Make')}
                      {th('model', 'Model')}
                      {th('color', 'Color')}
                      {th('review', 'Review')}
                      {showSite && th('site', 'Site')}
                      <th scope="col" className="px-3 py-3 text-center font-semibold">Evidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((row, i) => (
                      <tr key={row.id} className="border-t border-slate-200 hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800/50">
                        <td className="px-3 py-4 tabular-nums">{from + i}</td>
                        <td className="whitespace-nowrap px-3 py-4">{formatTime(row.offence_timestamp)}</td>
                        <td className="px-3 py-4">{labelOf(row.feature)}</td>
                        <td className="px-3 py-4 font-medium">{row.vrm || '—'}</td>
                        <td className="px-3 py-4">{row.day_night}</td>
                        <td className="px-3 py-4">{row.vehicle_make || '—'}</td>
                        <td className="px-3 py-4">{row.vehicle_model || '—'}</td>
                        <td className="px-3 py-4">{row.vehicle_color || '—'}</td>
                        <td className="px-3 py-4">
                          <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${row.review ? REVIEW_BADGE[row.review.human_label] : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300'}`}>
                            {row.review ? row.review.human_label : 'Not reviewed'}
                          </span>
                          {row.review?.image_clarity_clear === false && <span className="ml-1.5 text-xs text-slate-500">unclear</span>}
                        </td>
                        {showSite && <td className="px-3 py-4">{row.site_name}</td>}
                        <td className="px-3 py-4 text-center">
                          <button type="button" onClick={() => setViewerAt(i)} aria-label={`View evidence for ${row.vrm || 'event'} at ${formatTime(row.offence_timestamp)}`}
                            className="rounded p-1.5 text-slate-700 hover:bg-slate-200 dark:text-slate-200 dark:hover:bg-slate-700">
                            <EyeIcon />
                          </button>
                        </td>
                      </tr>
                    ))}
                    {!items.length && !loading && (
                      <tr>
                        <td colSpan={showSite ? 11 : 10} className="px-3 py-12 text-center text-slate-500 dark:text-slate-400">
                          {data?.site_total ? 'No records match these filters.' : 'No events are on the site right now.'}
                          {data && data.archived_total > 0 && (
                            <span className="mt-1 block text-xs">
                              {data.archived_total.toLocaleString()} more records (removed after a download, or imported from a spreadsheet) are kept for Analytics and the .xlsx.
                            </span>
                          )}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4 text-sm">
                <p>
                  Showing {from.toLocaleString()} to {to.toLocaleString()} of {total.toLocaleString()} entries
                  {data && data.archived_total > 0 && items.length > 0 && (
                    <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">
                      (+ {data.archived_total.toLocaleString()} archived or imported records: Analytics and .xlsx only)
                    </span>
                  )}
                </p>
                <nav aria-label="Pages" className="flex overflow-hidden rounded border border-slate-200 text-xs dark:border-slate-700">
                  <button type="button" onClick={() => setPage(page - 1)} disabled={page <= 1} className="px-3 py-2 text-indigo-600 hover:bg-slate-100 disabled:text-slate-400 dark:text-indigo-400 dark:hover:bg-slate-800">Previous</button>
                  {pageList(page, lastPage).map((p, i) => (p === '…'
                    ? <span key={`gap-${i}`} className="px-3 py-2 text-slate-400">…</span>
                    : (
                      <button key={p} type="button" onClick={() => setPage(p)} aria-current={p === page ? 'page' : undefined}
                        className={`border-l border-slate-200 px-3 py-2 dark:border-slate-700 ${p === page ? 'bg-indigo-600 font-semibold text-white' : 'text-indigo-600 hover:bg-slate-100 dark:text-indigo-400 dark:hover:bg-slate-800'}`}>
                        {p}
                      </button>
                    )))}
                  <button type="button" onClick={() => setPage(page + 1)} disabled={page >= lastPage} className="border-l border-slate-200 px-3 py-2 text-indigo-600 hover:bg-slate-100 disabled:text-slate-400 dark:border-slate-700 dark:text-indigo-400 dark:hover:bg-slate-800">Next</button>
                </nav>
              </div>
            </section>
          )}
        </main>

        {viewerAt !== null && items[viewerAt] && (
          <EvidenceViewer rows={items} index={viewerAt} onIndex={setViewerAt} onClose={() => setViewerAt(null)} labelOf={labelOf} formatTime={formatTime} />
        )}
        {deleting && (
          <DeleteSiteDialog
            site={{ id: deleting.site_id, name: deleting.site_name, events: deleting.overall.total_violations - deleting.overall.archived, archived: deleting.overall.archived }}
            onClose={() => setDeleting(null)}
            onDeleted={onDeleted}
          />
        )}
      </div>
    </>
  );
}

export default function SitesPage() {
  return (
    <RequireAuth>
      <SitesPageInner />
    </RequireAuth>
  );
}
