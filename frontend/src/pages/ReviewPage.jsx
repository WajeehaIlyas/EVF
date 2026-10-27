import Head from 'next/head';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import DecisionBar from '../components/reviewer/DecisionBar';
import FullImageViewer from '../components/reviewer/FullImageViewer';
import NavBar from '../components/NavBar';
import VerificationCard from '../components/reviewer/VerificationCard';
import useEventQueue from '../hooks/useEventQueue';
import useHotkeys from '../hooks/useHotkeys';
import { getAnalytics, getErrorMessage, getSites, submitReview } from '../services/api';
import { getAuth } from '../lib/auth';
import { ROUTES } from '../lib/routes';
import useReviewStore, {
  FEATURES,
  MMC_FIELDS,
  getMissingMmcChecks,
  selectCurrentEvent,
  selectRemaining,
  toReviewPayload,
} from '../store/reviewStore';
import RequireAuth from '../components/RequireAuth';

const PREFS_KEY = 'evf.reviewPrefs';
const FIELD_LABELS = { makeCorrect: 'Make', modelCorrect: 'Model', colorCorrect: 'Color' };

// ---------------------------------------------------------------------------- small UI bits
function Spinner({ className = 'h-8 w-8' }) {
  return (
    <div
      role="status"
      className={`animate-spin rounded-full border-4 border-slate-300 border-t-indigo-500 dark:border-slate-600 ${className}`}
    >
      <span className="sr-only">Loading…</span>
    </div>
  );
}

function StateCard({ title, children, actions }) {
  return (
    <div className="mx-auto mt-16 max-w-md rounded-xl border border-slate-200 bg-white p-8 text-center shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <h2 className="text-lg font-semibold">{title}</h2>
      <div className="mt-2 space-y-2 text-sm text-slate-600 dark:text-slate-300">{children}</div>
      {actions && <div className="mt-5 flex justify-center gap-2">{actions}</div>}
    </div>
  );
}

function PrimaryButton({ children, ...props }) {
  return (
    <button
      type="button"
      {...props}
      className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
    >
      {children}
    </button>
  );
}

function SecondaryButton({ children, ...props }) {
  return (
    <button
      type="button"
      {...props}
      className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-600 dark:text-slate-200 dark:hover:bg-slate-800"
    >
      {children}
    </button>
  );
}

function Chip({ children, tone = 'slate', title }) {
  const tones = {
    slate: 'bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200',
    indigo: 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/60 dark:text-indigo-200',
    amber: 'bg-amber-100 text-amber-800 dark:bg-amber-900/60 dark:text-amber-200',
    rose: 'bg-rose-100 text-rose-800 dark:bg-rose-900/60 dark:text-rose-200',
    emerald: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200',
  };
  return (
    <span title={title} className={`rounded-full px-2.5 py-1 text-xs font-semibold ${tones[tone]}`}>
      {children}
    </span>
  );
}

const fieldClass =
  'rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-sm text-slate-900 shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-300 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100';

// The reviews API still wants an email-shaped value for `reviewer_email`. Reviewers sign in with a
// username only, so the username is sent in that shape (name@evf.app); it is just an identifier.
const reviewerId = (username) => (!username ? '' : username.includes('@') ? username : `${username}@evf.app`);

function loadPrefs() {
  try {
    return JSON.parse(window.localStorage.getItem(PREFS_KEY) || '{}');
  } catch {
    return {};
  }
}

// ------------------------------------------------------------------------------- the page
function ReviewPageInner() {
  // ---- store
  const activeSiteId = useReviewStore((s) => s.activeSiteId);
  const activeFeature = useReviewStore((s) => s.activeFeature);
  const eventQueue = useReviewStore((s) => s.eventQueue);
  const currentIndex = useReviewStore((s) => s.currentIndex);
  const currentToggles = useReviewStore((s) => s.currentToggles);
  const skipMMCMode = useReviewStore((s) => s.skipMMCMode);
  const reviews = useReviewStore((s) => s.reviews);
  const remaining = useReviewStore(selectRemaining);
  const currentEvent = useReviewStore(selectCurrentEvent);
  const setActiveSiteId = useReviewStore((s) => s.setActiveSiteId);
  const setActiveFeature = useReviewStore((s) => s.setActiveFeature);
  const nextEvent = useReviewStore((s) => s.nextEvent);
  const previousEvent = useReviewStore((s) => s.previousEvent);
  const setToggle = useReviewStore((s) => s.setToggle);
  const resetToggles = useReviewStore((s) => s.resetToggles);
  const setSkipMMCMode = useReviewStore((s) => s.setSkipMMCMode);

  const queue = useEventQueue();

  // An import finished in another tab: top the queue up with the new events (non-destructive).
  const { refresh: refreshQueue } = queue;
  useEffect(() => {
    const onStorage = (event) => { if (event.key === 'evf.dataVersion') refreshQueue(); };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [refreshQueue]);

  // ---- local UI state
  const [sites, setSites] = useState([]);
  const [sitesStatus, setSitesStatus] = useState('loading'); // loading | ready | error
  const [sitesError, setSitesError] = useState(null);
  const [reviewer, setReviewer] = useState(''); // the signed-in username, saved with each review
  const [prefsLoaded, setPrefsLoaded] = useState(false);
  const [featureCounts, setFeatureCounts] = useState({}); // feature -> unreviewed events on the active site
  const [notice, setNotice] = useState(null); // { message, tone }
  const [missingFields, setMissingFields] = useState([]);
  const [flash, setFlash] = useState(null);

  const router = useRouter();
  const appliedQuery = useRef(null);
  const userPicked = useRef(null); // `${site}|${feature}` the reviewer chose by hand
  const noticeTimer = useRef(null);
  const flashTimer = useRef(null);
  const seqRef = useRef(0);
  const saveChains = useRef(new Map()); // eventId -> promise: saves for one event stay in order

  // ---- notifications
  const notify = useCallback((message, tone = 'info') => {
    clearTimeout(noticeTimer.current);
    setNotice({ message, tone });
    noticeTimer.current = setTimeout(() => setNotice(null), 4500);
  }, []);

  useEffect(() => () => {
    clearTimeout(noticeTimer.current);
    clearTimeout(flashTimer.current);
  }, []);

  // ---- preferences (reviewer email, site, feature, skip-MMC) and theme
  useEffect(() => {
    const prefs = loadPrefs();
    setReviewer(getAuth()?.username || '');
    if (prefs.skipMMCMode) setSkipMMCMode(true);
    if (prefs.siteId) setActiveSiteId(Number(prefs.siteId));
    if (prefs.feature) setActiveFeature(prefs.feature);
    setPrefsLoaded(true);
  }, [setActiveSiteId, setActiveFeature, setSkipMMCMode]);

  useEffect(() => {
    if (!prefsLoaded) return;
    try {
      window.localStorage.setItem(PREFS_KEY, JSON.stringify({
        siteId: activeSiteId, feature: activeFeature, skipMMCMode,
      }));
    } catch { /* storage unavailable: preferences just won't persist */ }
  }, [prefsLoaded, activeSiteId, activeFeature, skipMMCMode]);

  // A link such as /?site=3&feature=seatbelt (the Sites page) selects that site and feature.
  useEffect(() => {
    if (!prefsLoaded || !router.isReady) return;
    const { site, feature } = router.query;
    if (!site) return;
    const key = `${site}|${feature || ''}`;
    if (appliedQuery.current === key) return;
    appliedQuery.current = key;
    setActiveSiteId(Number(site));
    if (feature) setActiveFeature(String(feature));
  }, [prefsLoaded, router.isReady, router.query, setActiveSiteId, setActiveFeature]);

  // Unreviewed counts per feature for the chosen site. If the selected feature has nothing to
  // review (a remembered feature, or the wrong one for this site) switch to one that does, unless
  // the reviewer picked that feature by hand.
  const queueStatus = queue.status;
  useEffect(() => {
    if (!prefsLoaded || !activeSiteId) { setFeatureCounts({}); return undefined; }
    let cancelled = false;
    (async () => {
      let all;
      try { all = await getAnalytics(); } catch { return; }
      if (cancelled) return;
      const entry = all.find((x) => x.site_id === activeSiteId);
      const counts = {};
      (entry?.features || []).forEach((f) => { counts[f.feature] = f.stats.to_review; });
      setFeatureCounts(counts);

      const state = useReviewStore.getState();
      if (state.activeSiteId !== activeSiteId) return;
      const current = state.activeFeature;
      if (current && counts[current] > 0) return;
      if (current && userPicked.current === `${activeSiteId}|${current}`) return;
      const best = Object.entries(counts).sort((x, y) => y[1] - x[1])[0];
      if (best && best[1] > 0) setActiveFeature(best[0]);
    })();
    return () => { cancelled = true; };
  }, [prefsLoaded, activeSiteId, activeFeature, queueStatus, setActiveFeature]);

  // ---- sites
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
    // A remembered site that no longer exists must not stay selected.
    if (sitesStatus === 'ready' && activeSiteId && !sites.some((s) => s.id === activeSiteId)) {
      setActiveSiteId(null);
    }
  }, [sitesStatus, sites, activeSiteId, setActiveSiteId]);

  // The "missing checks" highlight belongs to one event only.
  useEffect(() => { setMissingFields([]); }, [currentEvent?.id]);

  // ---- saving reviews (optimistic: the UI advances immediately, the POST runs behind it) ----
  const sendReview = useCallback((eventId, payload, meta) => {
    const seq = (seqRef.current += 1);
    const store = useReviewStore.getState();
    store.recordReview(eventId, { ...meta, payload, seq, status: 'saving', error: null });

    const previous = saveChains.current.get(eventId) ?? Promise.resolve();
    const task = previous.then(async () => {
      const isLatest = () => useReviewStore.getState().reviews[eventId]?.seq === seq;
      try {
        const result = await submitReview(payload);
        if (isLatest()) {
          useReviewStore.getState().recordReview(eventId, {
            status: 'saved', error: null, reviewedAt: result.reviewed_at,
          });
        }
      } catch (err) {
        const message = getErrorMessage(err);
        if (isLatest()) useReviewStore.getState().recordReview(eventId, { status: 'error', error: message });
        notify(`Case ${meta.caseId} was not saved: ${message}`, 'error');
      }
    });
    saveChains.current.set(eventId, task);
    task.finally(() => {
      if (saveChains.current.get(eventId) === task) saveChains.current.delete(eventId);
    });
  }, [notify]);

  const retryFailed = useCallback(() => {
    const all = useReviewStore.getState().reviews;
    Object.entries(all).forEach(([eventId, entry]) => {
      if (entry.status === 'error' && entry.payload) {
        sendReview(Number(eventId), { ...entry.payload, reviewer_email: reviewerId(reviewer) }, {
          humanLabel: entry.humanLabel, toggles: entry.toggles, caseId: entry.caseId,
        });
      }
    });
  }, [sendReview, reviewer]);

  const submitDecision = useCallback((label) => {
    const state = useReviewStore.getState();
    const event = state.eventQueue[state.currentIndex];
    if (!event) return;

    if (!reviewer) {
      notify('Your session has ended. Sign in again.', 'error');
      return;
    }

    if (label === 'TP' && !state.skipMMCMode) {
      const missing = getMissingMmcChecks(event, state.currentToggles);
      if (missing.length > 0) {
        setMissingFields(missing);
        notify(
          `Answer ${missing.map((f) => FIELD_LABELS[f]).join(', ')} first — or press A for all correct, `
          + 'or S to skip MMC review.',
          'error',
        );
        return;
      }
    }

    setMissingFields([]);
    const toggles = { ...state.currentToggles };
    sendReview(
      event.id,
      toReviewPayload({ event, label, toggles, skipMMC: state.skipMMCMode, reviewerEmail: reviewerId(reviewer) }),
      { humanLabel: label, toggles, caseId: event.case_id },
    );

    clearTimeout(flashTimer.current);
    setFlash(label);
    flashTimer.current = setTimeout(() => setFlash(null), 220);
    state.nextEvent(); // advance immediately; the images are already preloaded
  }, [reviewer, notify, sendReview]);

  // ---- hotkey handlers ------------------------------------------------------------------
  const handleToggleKey = useCallback((field) => {
    const state = useReviewStore.getState();
    if (!state.eventQueue[state.currentIndex]) return;
    if (state.skipMMCMode && MMC_FIELDS.includes(field)) {
      notify('Make / model / color checks are skipped. Press S to turn Skip MMC off.');
      return;
    }
    state.cycleToggle(field);
    setMissingFields((prev) => prev.filter((f) => f !== field));
  }, [notify]);

  const handleAllCorrect = useCallback(() => {
    const state = useReviewStore.getState();
    if (!state.eventQueue[state.currentIndex]) return;
    const next = { vrmCorrect: true, imageClarityClear: true };
    if (!state.skipMMCMode) Object.assign(next, { makeCorrect: true, modelCorrect: true, colorCorrect: true });
    state.setToggles(next);
    setMissingFields([]);
  }, []);

  const handleToggleSkip = useCallback(() => {
    const next = !useReviewStore.getState().skipMMCMode;
    setSkipMMCMode(next);
    setMissingFields([]);
    notify(next ? 'Skip MMC Review: ON' : 'Skip MMC Review: OFF');
  }, [setSkipMMCMode, notify]);

  useHotkeys(
    {
      onDecision: submitDecision,
      onNext: nextEvent,
      onPrevious: previousEvent,
      onToggle: handleToggleKey,
      onAllCorrect: handleAllCorrect,
      onResetToggles: resetToggles,
      onToggleSkipMMC: handleToggleSkip,
    },
    { enabled: queue.status === 'ready' },
  );

  // ---- derived values ---------------------------------------------------------------------
  const reviewList = useMemo(() => Object.values(reviews), [reviews]);
  const savingCount = reviewList.filter((r) => r.status === 'saving').length;
  const failedCount = reviewList.filter((r) => r.status === 'error').length;
  const currentReview = currentEvent ? reviews[currentEvent.id] : undefined;

  // Warn before leaving while reviews are still being written or failed to save.
  useEffect(() => {
    if (savingCount + failedCount === 0) return undefined;
    const onBeforeUnload = (event) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [savingCount, failedCount]);

  const hint = skipMMCMode
    ? 'Skip MMC is ON: make / model / color are not recorded · S turns it off'
    : 'TP needs make / model / color answered: M · O · C — or A for all correct';

  const otherFeatures = Object.entries(featureCounts).filter(([f, n]) => n > 0 && f !== activeFeature);

  // ---- main content -------------------------------------------------------------------------
  let body;
  if (!activeSiteId || !activeFeature) {
    body = (
      <StateCard title="Choose what to review">
        {sitesStatus === 'loading' && <Spinner className="mx-auto h-6 w-6" />}
        {sitesStatus === 'error' && <p className="text-rose-600 dark:text-rose-400">{sitesError}</p>}
        {sitesStatus === 'ready' && sites.length === 0 && (
          <p>No sites exist yet. Create one with <code>POST /api/v1/sites</code> and ingest some packages.</p>
        )}
        {sitesStatus === 'ready' && sites.length > 0 && <p>Pick a site and a feature in the header to load its queue.</p>}
      </StateCard>
    );
  } else if (queue.isLoading) {
    body = (
      <div className="mt-24 flex flex-col items-center gap-3 text-slate-600 dark:text-slate-300">
        <Spinner className="h-10 w-10" />
        <p className="text-sm">Loading review queue…</p>
      </div>
    );
  } else if (queue.status === 'error') {
    body = (
      <StateCard title="Couldn't load the queue" actions={<PrimaryButton onClick={queue.reload}>Try again</PrimaryButton>}>
        <p className="text-rose-600 dark:text-rose-400">{queue.error}</p>
      </StateCard>
    );
  } else if (eventQueue.length === 0) {
    body = (
      <StateCard
        title="Queue is empty"
        actions={(
          <>
            <SecondaryButton onClick={queue.reload}>Check again</SecondaryButton>
            <Link href={ROUTES.analytics}
              className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-100 dark:border-slate-600 dark:text-slate-200 dark:hover:bg-slate-800">
              View analytics
            </Link>
          </>
        )}
      >
        <p>There are no unreviewed events for this site and feature.</p>
        {otherFeatures.length > 0 && (
          <div className="pt-2">
            <p className="mb-2">This site has unreviewed events in:</p>
            <div className="flex flex-wrap justify-center gap-2">
              {otherFeatures.map(([feature, count]) => (
                <PrimaryButton key={feature} onClick={() => setActiveFeature(feature)}>
                  {FEATURES.find((f) => f.value === feature)?.label || feature} ({count})
                </PrimaryButton>
              ))}
            </div>
          </div>
        )}
      </StateCard>
    );
  } else if (!currentEvent) {
    const skipped = eventQueue.filter((e) => !reviews[e.id]).length;
    body = queue.isRefilling || !queue.exhausted ? (
      <div className="mt-24 flex flex-col items-center gap-3 text-slate-600 dark:text-slate-300">
        <Spinner className="h-10 w-10" />
        <p className="text-sm">{queue.refillError ? `Could not load more: ${queue.refillError}` : 'Loading more events…'}</p>
        {queue.refillError && <SecondaryButton onClick={queue.reload}>Reload queue</SecondaryButton>}
      </div>
    ) : (
      <StateCard
        title="You're all caught up 🎉"
        actions={(
          <>
            <SecondaryButton onClick={previousEvent}>← Go back</SecondaryButton>
            <PrimaryButton onClick={queue.reload} disabled={savingCount > 0}>Reload queue</PrimaryButton>
            <Link href={ROUTES.analytics}
              className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-100 dark:border-slate-600 dark:text-slate-200 dark:hover:bg-slate-800">
              View analytics
            </Link>
          </>
        )}
      >
        <p>You reached the end of the queue.</p>
        {skipped > 0 && (
          <p className="font-medium text-amber-600 dark:text-amber-400">
            {skipped} event{skipped === 1 ? ' was' : 's were'} skipped without a decision — reload to see
            {skipped === 1 ? ' it' : ' them'} again.
          </p>
        )}
        {savingCount > 0 && <p>Waiting for {savingCount} save{savingCount === 1 ? '' : 's'} to finish…</p>}
      </StateCard>
    );
  } else {
    body = (
      <div className="grid gap-3 lg:grid-cols-5">
        <section aria-label="Full image" className="h-[50vh] min-h-[320px] lg:col-span-3 lg:h-[calc(100vh-17rem)]">
          <FullImageViewer
            src={currentEvent.full_image_url}
            alt={`Full image for case ${currentEvent.case_id}`}
          />
        </section>
        <section aria-label="Details and verification"
          className="flex flex-col gap-3 lg:col-span-2 lg:h-[calc(100vh-17rem)] lg:overflow-y-auto lg:pr-1">
          <VerificationCard
            event={currentEvent}
            toggles={currentToggles}
            skipMMC={skipMMCMode}
            missingFields={missingFields}
            onToggle={(field, value) => {
              setToggle(field, value);
              setMissingFields((prev) => prev.filter((f) => f !== field));
            }}
            onSkipMMCChange={(value) => { setSkipMMCMode(value); setMissingFields([]); }}
          />
        </section>
      </div>
    );
  }

  return (
    <>
      <Head>
        <title>Review · EVF</title>
      </Head>

      <div className="min-h-screen bg-slate-100 pb-28 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
        {/* ------------------------------------------------------------------ header */}
        <NavBar current="review" />
        <div className="sticky top-[49px] z-20 border-b border-slate-200 bg-white/95 backdrop-blur dark:border-slate-700 dark:bg-slate-900/95">
          <div className="mx-auto flex max-w-screen-2xl flex-wrap items-center gap-x-4 gap-y-2 px-3 py-2.5">
            <label className="flex items-center gap-2 text-xs text-slate-600 dark:text-slate-300">
              Site
              <select
                className={fieldClass}
                value={activeSiteId ?? ''}
                disabled={sitesStatus !== 'ready'}
                onChange={(e) => {
                  userPicked.current = null;
                  setActiveSiteId(e.target.value ? Number(e.target.value) : null);
                  e.target.blur(); // hand the keyboard back to the hotkeys
                }}
              >
                <option value="">{sitesStatus === 'loading' ? 'Loading…' : 'Select site…'}</option>
                {sites.map((site) => (
                  <option key={site.id} value={site.id}>{site.name} ({site.code})</option>
                ))}
              </select>
            </label>

            <label className="flex items-center gap-2 text-xs text-slate-600 dark:text-slate-300">
              Feature
              <select
                className={fieldClass}
                value={activeFeature ?? ''}
                onChange={(e) => {
                  userPicked.current = `${activeSiteId}|${e.target.value}`;
                  setActiveFeature(e.target.value || null);
                  e.target.blur();
                }}
              >
                <option value="">Select feature…</option>
                {FEATURES.map((f) => (
                  <option key={f.value} value={f.value}>
                    {f.label}{featureCounts[f.value] != null ? ` (${featureCounts[f.value]})` : ''}
                  </option>
                ))}
              </select>
            </label>

            {sitesStatus === 'error' && (
              <button type="button" onClick={loadSites}
                className="text-xs font-medium text-rose-600 underline dark:text-rose-400"
                title={sitesError}>
                Couldn&apos;t load sites — retry
              </button>
            )}

          </div>

          {/* event strip */}
          {currentEvent && queue.status === 'ready' && (
            <div className="border-t border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900">
              <div className="mx-auto flex max-w-screen-2xl flex-wrap items-center gap-x-4 gap-y-1 px-3 py-2 text-sm">
                <span className="font-mono text-xs text-slate-500 dark:text-slate-400">Case {currentEvent.case_id}</span>
                <span className="rounded bg-yellow-300 px-2 py-0.5 font-mono text-sm font-bold tracking-wider text-slate-900">
                  {currentEvent.vrm || 'NO VRM'}
                </span>
                <span className="text-xs text-slate-600 dark:text-slate-300">
                  {String(currentEvent.offence_timestamp).replace('T', ' ')}
                </span>
                <Chip tone={currentEvent.day_night === 'Day' ? 'amber' : 'indigo'}>
                  {currentEvent.day_night === 'Day' ? '☀ Day' : '☾ Night'}
                </Chip>
                {currentReview?.humanLabel && (
                  <Chip tone="emerald" title="Decision already made this session; deciding again overwrites it">
                    Already marked {currentReview.humanLabel}
                  </Chip>
                )}

                <div className="ml-auto flex items-center gap-2">
                  <Chip title="Position in the loaded queue">{currentIndex + 1} / {eventQueue.length}</Chip>
                  <Chip tone="indigo" title="Estimated unreviewed events remaining">{remaining} left</Chip>
                  {queue.isRefilling && <Chip>Loading more…</Chip>}
                  {savingCount > 0 && <Chip tone="amber">Saving {savingCount}…</Chip>}
                  {failedCount > 0 && (
                    <button type="button" onClick={retryFailed}
                      className="rounded-full bg-rose-600 px-2.5 py-1 text-xs font-semibold text-white hover:bg-rose-500">
                      {failedCount} not saved — retry
                    </button>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>

        {/* -------------------------------------------------------------------- body */}
        <main className="mx-auto max-w-screen-2xl p-3">{body}</main>

        {/* toast */}
        {notice && (
          <div
            role="status"
            aria-live="polite"
            className={`fixed left-1/2 top-24 z-40 max-w-lg -translate-x-1/2 rounded-lg px-4 py-2.5 text-sm font-medium text-white shadow-lg ${
              notice.tone === 'error' ? 'bg-rose-600' : 'bg-slate-800 dark:bg-slate-700'
            }`}
          >
            {notice.message}
          </div>
        )}

        <DecisionBar
          onDecision={submitDecision}
          onPrevious={previousEvent}
          onNext={nextEvent}
          canPrevious={currentIndex > 0}
          canNext={queue.status === 'ready' && currentIndex < eventQueue.length}
          disabled={!currentEvent || queue.status !== 'ready'}
          selectedLabel={currentReview?.humanLabel ?? null}
          flash={flash}
          hint={currentEvent ? hint : ''}
        />
      </div>
    </>
  );
}

export default function ReviewPage() {
  return (
    <RequireAuth>
      <ReviewPageInner />
    </RequireAuth>
  );
}
