/**
 * Zustand store for the review workspace.
 *
 * Toggle values mirror the backend: true = tick, false = cross, null = not answered/skipped.
 */
import { create } from 'zustand';

export const FEATURES = [
  { value: 'seatbelt', label: 'Seatbelt' },
  { value: 'mobilephone', label: 'Mobile Phone' },
  { value: 'alpr', label: 'ALPR' },
  { value: 'redlight', label: 'Red Light' },
  { value: 'bannedturn', label: 'Banned Turn' },
  { value: 'nearmiss', label: 'Near Miss' },
];

export const TOGGLE_FIELDS = [
  'vrmCorrect',
  'makeCorrect',
  'modelCorrect',
  'colorCorrect',
  'imageClarityClear',
];
/** The make / model / colour checks that "Skip MMC Review" turns off. */
export const MMC_FIELDS = ['makeCorrect', 'modelCorrect', 'colorCorrect'];

export const EMPTY_TOGGLES = Object.freeze({
  vrmCorrect: null,
  makeCorrect: null,
  modelCorrect: null,
  colorCorrect: null,
  imageClarityClear: null,
});

const API_FIELD = {
  vrmCorrect: 'vrm_correct',
  makeCorrect: 'make_correct',
  modelCorrect: 'model_correct',
  colorCorrect: 'color_correct',
  imageClarityClear: 'image_clarity_clear',
};

// ----------------------------------------------------------------------- pure helpers
/** null -> tick -> cross -> null */
const nextToggleValue = (value) => (value === null ? true : value === true ? false : null);

function togglesFromReview(review) {
  if (!review) return { ...EMPTY_TOGGLES };
  return {
    vrmCorrect: review.vrm_correct ?? null,
    makeCorrect: review.make_correct ?? null,
    modelCorrect: review.model_correct ?? null,
    colorCorrect: review.color_correct ?? null,
    imageClarityClear: review.image_clarity_clear ?? null,
  };
}

/** Toggles to show for an event: this session's decision, else any saved review, else blank. */
function togglesForEvent(event, reviews) {
  if (!event) return { ...EMPTY_TOGGLES };
  const local = reviews[event.id];
  if (local?.toggles) return { ...local.toggles };
  return togglesFromReview(event.review);
}

/**
 * MMC checks that must still be answered before a True Positive can be submitted.
 * Only checks for values the model actually extracted are required.
 */
export function getMissingMmcChecks(event, toggles) {
  if (!event) return [];
  const extracted = {
    makeCorrect: Boolean(event.vehicle_make),
    modelCorrect: Boolean(event.vehicle_model),
    colorCorrect: Boolean(event.vehicle_color),
  };
  return MMC_FIELDS.filter((field) => extracted[field] && toggles[field] === null);
}

/** Build the POST /reviews/ body. In skip-MMC mode the make/model/colour checks are sent as null. */
export function toReviewPayload({ event, label, toggles, skipMMC, reviewerEmail }) {
  const effective = { ...toggles };
  if (skipMMC) MMC_FIELDS.forEach((field) => { effective[field] = null; });

  const payload = {
    event_id: event.id,
    human_label: label,
    reviewer_email: reviewerEmail.trim(),
  };
  TOGGLE_FIELDS.forEach((field) => { payload[API_FIELD[field]] = effective[field] ?? null; });
  return payload;
}

const blankQueueState = () => ({
  eventQueue: [],
  currentIndex: 0,
  currentToggles: { ...EMPTY_TOGGLES },
  serverTotal: 0,
  reviews: {},
  savedCount: 0,
  syncSavedCount: 0,
});

// ---------------------------------------------------------------------------- the store
const useReviewStore = create((set, get) => ({
  // --- context
  activeSiteId: null,
  activeFeature: null,

  // --- queue
  eventQueue: [],
  currentIndex: 0, // may equal eventQueue.length = "past the end of what is loaded"
  serverTotal: 0, // unreviewed events on the server at the last sync

  // --- current review form
  currentToggles: { ...EMPTY_TOGGLES },
  skipMMCMode: false,

  // --- this session's decisions: eventId -> { humanLabel, toggles, payload, seq, status, error }
  reviews: {},
  savedCount: 0,
  syncSavedCount: 0,

  // ------------------------------------------------------------------ context actions
  setActiveSiteId: (siteId) =>
    set((s) => (s.activeSiteId === siteId ? s : { activeSiteId: siteId, ...blankQueueState() })),

  setActiveFeature: (feature) =>
    set((s) => (s.activeFeature === feature ? s : { activeFeature: feature, ...blankQueueState() })),

  // ------------------------------------------------------------------- queue actions
  /** Replace the queue (initial load / reload). */
  setQueue: (events, total) =>
    set((s) => ({
      eventQueue: events,
      currentIndex: 0,
      currentToggles: togglesForEvent(events[0], {}),
      serverTotal: total ?? events.length,
      reviews: {},
      syncSavedCount: s.savedCount,
    })),

  /** Append newly fetched events (de-duplicated by id). Returns how many were new. */
  appendEvents: (events, total) => {
    const before = get().eventQueue.length;
    set((s) => {
      const known = new Set(s.eventQueue.map((e) => e.id));
      const fresh = events.filter((e) => !known.has(e.id));
      return {
        eventQueue: fresh.length ? [...s.eventQueue, ...fresh] : s.eventQueue,
        serverTotal: total ?? s.serverTotal,
        syncSavedCount: s.savedCount,
      };
    });
    return get().eventQueue.length - before;
  },

  nextEvent: () =>
    set((s) => {
      if (s.currentIndex >= s.eventQueue.length) return s;
      const index = s.currentIndex + 1;
      return { currentIndex: index, currentToggles: togglesForEvent(s.eventQueue[index], s.reviews) };
    }),

  previousEvent: () =>
    set((s) => {
      if (s.currentIndex <= 0) return s;
      const index = s.currentIndex - 1;
      return { currentIndex: index, currentToggles: togglesForEvent(s.eventQueue[index], s.reviews) };
    }),

  // ------------------------------------------------------------------ toggle actions
  setToggle: (field, value) =>
    set((s) => (TOGGLE_FIELDS.includes(field)
      ? { currentToggles: { ...s.currentToggles, [field]: value } }
      : s)),

  /** Hotkey behaviour: unanswered -> tick -> cross -> unanswered. */
  cycleToggle: (field) =>
    set((s) => (TOGGLE_FIELDS.includes(field)
      ? { currentToggles: { ...s.currentToggles, [field]: nextToggleValue(s.currentToggles[field]) } }
      : s)),

  setToggles: (partial) => set((s) => ({ currentToggles: { ...s.currentToggles, ...partial } })),

  resetToggles: () => set({ currentToggles: { ...EMPTY_TOGGLES } }),

  setSkipMMCMode: (value) => set({ skipMMCMode: Boolean(value) }),

  // ----------------------------------------------------------------- review tracking
  /** Merge `entry` into the session record for an event (and count first successful saves). */
  recordReview: (eventId, entry) =>
    set((s) => {
      const previous = s.reviews[eventId];
      const becameSaved = entry.status === 'saved' && !previous?.everSaved;
      return {
        reviews: {
          ...s.reviews,
          [eventId]: { ...previous, ...entry, everSaved: Boolean(previous?.everSaved) || becameSaved },
        },
        savedCount: becameSaved ? s.savedCount + 1 : s.savedCount,
      };
    }),
}));

export const selectCurrentEvent = (s) => s.eventQueue[s.currentIndex] ?? null;

/** Estimated unreviewed events left, adjusting the last server count for saves since then. */
export const selectRemaining = (s) =>
  Math.max(0, s.serverTotal - (s.savedCount - s.syncSavedCount));

export default useReviewStore;
