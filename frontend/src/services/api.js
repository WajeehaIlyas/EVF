/**
 * Axios client for the EVF REST API (FastAPI backend, Phase 2).
 * Override the base URL with NEXT_PUBLIC_API_URL when deploying.
 */
import axios from 'axios';

import { clearAuth, getAuth } from '../lib/auth';

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000/api/v1';

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
});

// Every request carries the signed-in user's token. A 401 on anything but the login itself means
// the session ended: forget it and go to the login page.
api.interceptors.request.use((config) => {
  const auth = typeof window !== 'undefined' ? getAuth() : null;
  if (auth) config.headers.Authorization = `Bearer ${auth.token}`;
  return config;
});
api.interceptors.response.use(
  (response) => response,
  (error) => {
    const url = error?.config?.url || '';
    if (error?.response?.status === 401 && !url.includes('/auth/login') && typeof window !== 'undefined') {
      clearAuth();
      if (!window.location.pathname.startsWith('/login')) {
        window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
      }
    }
    return Promise.reject(error);
  },
);

/** POST /analytics/auth/login -> { access_token, username, expires_at } */
export async function login(username, password) {
  const { data } = await api.post('/analytics/auth/login', { username, password });
  return data;
}

/** Turn any Axios/FastAPI error into a short, human-readable message. */
export function getErrorMessage(error) {
  const detail = error?.response?.data?.detail;
  if (error?.response?.status === 404 && detail === 'Not Found') {
    // FastAPI's answer for a URL it has no route for (not for "this record does not exist")
    return 'The server does not have this endpoint. Update the backend files and restart it.';
  }
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{ loc: ['body', 'reviewer_email'], msg: '...' }]
    return detail
      .map((d) => `${(d.loc || []).slice(1).join('.') || 'request'}: ${d.msg}`)
      .join('; ');
  }
  if (error?.response?.status === 404 && (detail === 'Not Found' || detail === undefined)) {
    // FastAPI's own "no such route" answer: the backend is running without the newest files.
    return 'The server has no such endpoint. Install the latest backend files and restart the backend.';
  }
  if (error?.response) return `Server error (HTTP ${error.response.status})`;
  if (error?.code === 'ECONNABORTED') return 'The request timed out';
  if (error?.request) return `Cannot reach the API at ${API_BASE_URL}. Is the backend running?`;
  return error?.message || 'Unexpected error';
}

/** True when a request was aborted via AbortController. */
export function isRequestCancelled(error) {
  return error?.code === 'ERR_CANCELED' || axios.isCancel(error);
}

/** GET /sites -> [{ id, name, code, created_at }] */
export async function getSites() {
  const { data } = await api.get('/sites');
  return data;
}

/**
 * GET /events/queue -> { items, total, limit, offset, has_more }
 *
 * `page` is 1-based and converted to the API's offset. NOTE: the unreviewed queue shrinks as
 * events are reviewed, so the review workspace always asks for page 1 and de-duplicates.
 */
export async function getEventQueue(
  siteId,
  feature,
  page = 1,
  limit = 50,
  { status = 'unreviewed', signal } = {},
) {
  const offset = (Math.max(1, page) - 1) * limit;
  const { data } = await api.get('/events/queue', {
    params: { site_id: siteId, feature, status, limit, offset },
    signal,
  });
  return data;
}

/**
 * POST /reviews/ (create-or-update).
 * payload: { event_id, human_label: 'TP'|'FP'|'Unsure', reviewer_email,
 *            vrm_correct, make_correct, model_correct, color_correct, image_clarity_clear }
 * Toggle values: true = tick, false = cross, null = skipped.
 */
export async function submitReview(payload) {
  const { data } = await api.post('/reviews/', payload);
  return data;
}

/** POST /ingest/local { site_id, feature, folder_path, recursive } - blocks until finished. */
export async function triggerLocalIngest(data) {
  const response = await api.post('/ingest/local', data, { timeout: 0 });
  return response.data;
}

/**
 * POST /ingest/sftp { site_id, feature, host, port, username, password | key_path,
 *                     remote_directory } -> { job_id, status, ws_path }
 */
export async function triggerSftpIngest(data) {
  const response = await api.post('/ingest/sftp', data);
  return response.data;
}

export default api;

// ------------------------------------------------------------- Phase 4: analytics & exports

/**
 * GET /metrics/summary. `falseNegatives` (optional, >= 0) is the count of missed detections;
 * without it the API cannot compute recall and returns recall_pct = null.
 */
export async function getMetricsSummary(siteId, feature, { falseNegatives, signal } = {}) {
  const params = { site_id: siteId };
  if (feature) params.feature = feature;
  if (falseNegatives !== null && falseNegatives !== undefined && falseNegatives !== '') {
    params.false_negatives = falseNegatives;
  }
  const { data } = await api.get('/metrics/summary', { params, signal });
  return data;
}

/** GET /export/status -> { google_sheets_configured, service_account_email, detail } */
export async function getExportStatus() {
  const { data } = await api.get('/export/status');
  return data;
}

/**
 * POST /export/google-sheets
 * payload: { spreadsheet_id (ID or URL), site_id, feature|null, layout: '9-col'|'16-col',
 *            worksheet_name|null }
 */
export async function exportToGoogleSheets(payload) {
  const { data } = await api.post('/export/google-sheets', payload, { timeout: 120000 });
  return data;
}

/** GET /export/csv and hand the file to the browser as a download. Resolves to the file name. */
export async function downloadCsv({ siteId, feature, layout = '9-col' }) {
  const params = { site_id: siteId, layout };
  if (feature) params.feature = feature;

  let response;
  try {
    response = await api.get('/export/csv', { params, responseType: 'blob', timeout: 120000 });
  } catch (error) {
    // With responseType 'blob' even error bodies arrive as Blobs; unwrap so getErrorMessage works.
    const body = error?.response?.data;
    if (typeof Blob !== 'undefined' && body instanceof Blob) {
      try {
        error.response.data = JSON.parse(await body.text());
      } catch { /* not JSON: keep the generic message */ }
    }
    throw error;
  }

  const disposition = response.headers?.['content-disposition'] || '';
  const match = /filename="?([^";]+)"?/i.exec(disposition);
  const filename = match ? match[1] : `evf_export_${layout}.csv`;

  const url = URL.createObjectURL(response.data);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  return filename;
}

// ------------------------------------------------------- imports (browser uploads) & site stats

/** GET /sites/stats -> sites with event_count, reviewed_count, pending_count, last_ingested_at */
export async function getSiteStats() {
  const { data } = await api.get('/sites/stats');
  return data;
}

/**
 * POST /imports. Append:  { feature, site_id, timezone }
 *                Create:  { feature, new_site: { name, code? }, timezone }
 * -> { id, site_id, site_name, site_code, created_site, status: 'uploading', ... }
 */
export async function createImport(payload) {
  const { data } = await api.post('/imports', payload);
  return data;
}

/**
 * POST /imports/{id}/files: upload one batch of File objects. The relative path of each file
 * (webkitRelativePath when a folder was chosen) is sent alongside so sub-folders are preserved.
 * onProgress(loadedBytes) reports progress for this batch only.
 */
export async function uploadImportFiles(importId, items, { onProgress, signal } = {}) {
  // items: [{ file: File, path: '<role>/<relative path>' }] with role data | full | crop
  const form = new FormData();
  items.forEach(({ file, path }) => {
    form.append('files', file, file.name);
    form.append('paths', path);
  });
  const { data } = await api.post(`/imports/${importId}/files`, form, {
    timeout: 0,
    signal,
    headers: { 'Content-Type': 'multipart/form-data' },
    onUploadProgress: (event) => onProgress?.(event.loaded),
  });
  return data;
}

/** POST /imports/{id}/finish: queue ingestion. */
export async function finishImport(importId) {
  const { data } = await api.post(`/imports/${importId}/finish`, null, { timeout: 0 });
  return data;
}

/** GET /imports/{id}: status is uploading | queued | processing | completed | failed. */
export async function getImport(importId) {
  const { data } = await api.get(`/imports/${importId}`);
  return data;
}

/** DELETE /imports/{id}: cancel an upload that has not been finished. */
export async function cancelImport(importId) {
  const { data } = await api.delete(`/imports/${importId}`);
  return data;
}

/** GET /imports: recent imports, newest first. */
export async function listImports({ siteId, limit = 10 } = {}) {
  const params = { limit };
  if (siteId) params.site_id = siteId;
  const { data } = await api.get('/imports', { params });
  return data;
}

// ------------------------------------------------------- analytics, spreadsheet import, zip export

/**
 * GET /analytics/sites -> [{ site_id, site_name, site_code, overall: stats,
 *                            features: [{ feature, stats }] }]
 * Every site is listed (even one with no events) and the figures include events that were
 * removed after a zip download and rows imported from a spreadsheet.
 */
export async function getAnalytics({ signal } = {}) {
  const { data } = await api.get('/analytics/sites', { signal });
  return data;
}

/**
 * POST /analytics/sites/{id}/import-xlsx (multipart). One sheet per feature, named after it.
 * -> { added, updated, sheets: [{ sheet, feature, rows_read, added, updated, unchanged,
 *                                  unlabelled, duplicates, invalid, problems, skipped_reason }] }
 */
export async function importSiteXlsx(siteId, file, { feature, onProgress } = {}) {
  const form = new FormData();
  form.append('file', file, file.name);
  if (feature) form.append('feature', feature);
  const { data } = await api.post(`/analytics/sites/${siteId}/import-xlsx`, form, {
    timeout: 0,
    headers: { 'Content-Type': 'multipart/form-data' },
    onUploadProgress: (event) => onProgress?.(event.loaded, event.total),
  });
  return data;
}

/** GET /analytics/classified-exports/preview -> { reviewed, unreviewed, counts: { 'TP/clear': n, ... } } */
export async function getClassifiedPreview(siteId, feature) {
  const params = { site_id: siteId };
  if (feature) params.feature = feature;
  const { data } = await api.get('/analytics/classified-exports/preview', { params });
  return data;
}

/**
 * POST /analytics/classified-exports: build the zip on the server.
 * -> { token, name, filename, events, counts, missing_images, zip_bytes }
 */
export async function createClassifiedExport({ siteId, feature, name, includeCrops = false }) {
  const { data } = await api.post(
    '/analytics/classified-exports',
    { site_id: siteId, feature: feature || null, name: name || null, include_crops: includeCrops },
    { timeout: 0 },
  );
  return data;
}

/** URL of the built zip; point a link at it so the browser streams the file to disk. */
export function classifiedExportDownloadUrl(token) {
  return `${API_BASE_URL}/analytics/classified-exports/${token}/download`;
}

/** POST /analytics/classified-exports/{token}/finalize -> { archived, skipped_changed, files_removed } */
export async function finalizeClassifiedExport(token) {
  const { data } = await api.post(`/analytics/classified-exports/${token}/finalize`, null, { timeout: 0 });
  return data;
}

/** DELETE /analytics/classified-exports/{token}: drop a built zip without removing anything. */
export async function discardClassifiedExport(token) {
  await api.delete(`/analytics/classified-exports/${token}`);
}

// ------------------------------------------------------- sites page: browse, delete, .xlsx download

/** Origin that serves the stored images (the API host without /api/v1). Override with NEXT_PUBLIC_MEDIA_URL. */
const MEDIA_ORIGIN = (process.env.NEXT_PUBLIC_MEDIA_URL || API_BASE_URL.replace(/\/api\/v\d+\/?$/, '')).replace(/\/$/, '');

/** '/media/site/feature/full/x.jpg' -> 'http://localhost:8000/media/site/feature/full/x.jpg' */
export function mediaUrl(path) {
  if (!path) return '';
  if (/^(https?:)?\/\//i.test(path) || path.startsWith('data:')) return path;
  return `${MEDIA_ORIGIN}${path.startsWith('/') ? '' : '/'}${path}`;
}

/**
 * GET /analytics/browse/events -> { items, total, site_total, archived_total, features, limit, offset }
 * params: { siteId, feature, plate, search, dayNight, review, dateFrom, dateTo, sort, order, limit, offset }
 * review: 'reviewed' | 'unreviewed' | 'TP' | 'FP' | 'Unsure'. Dates are naive ISO strings (site wall-clock).
 */
export async function browseEvents(params = {}, { signal } = {}) {
  const query = {
    site_id: params.siteId, feature: params.feature, plate: params.plate, search: params.search,
    day_night: params.dayNight, review: params.review, date_from: params.dateFrom, date_to: params.dateTo,
    sort: params.sort, order: params.order, limit: params.limit, offset: params.offset,
  };
  Object.keys(query).forEach((key) => {
    if (query[key] === undefined || query[key] === null || query[key] === '') delete query[key];
  });
  const { data } = await api.get('/analytics/browse/events', { params: query, signal });
  return data;
}

/** DELETE /analytics/sites/{id}: removes the site, its events, reviews, images and analytics history. */
export async function deleteSite(siteId) {
  const { data } = await api.delete(`/analytics/sites/${siteId}`, { timeout: 0 });
  return data;
}

/** URL of the site's workbook (one sheet per feature, or just `feature`); link to it to download. */
export function siteXlsxUrl(siteId, feature) {
  const query = feature ? `?feature=${encodeURIComponent(feature)}` : '';
  return `${API_BASE_URL}/analytics/sites/${siteId}/export.xlsx${query}`;
}

/**
 * PUT /analytics/events/{id}/review: save (or change) the verdict of any event, reviewed or not.
 * payload: { human_label: 'TP'|'FP'|'Unsure', reviewer_email?, vrm_correct, make_correct,
 *            model_correct, color_correct, image_clarity_clear }   (true tick, false cross, null skipped)
 */
export async function saveEventReview(eventId, payload) {
  const { data } = await api.put(`/analytics/events/${eventId}/review`, payload);
  return data;
}

/** DELETE /analytics/events/{id}/review: the event goes back to "not reviewed". */
export async function clearEventReview(eventId) {
  await api.delete(`/analytics/events/${eventId}/review`);
}
