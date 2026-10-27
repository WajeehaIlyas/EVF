/**
 * Loads the review queue for the active site/feature, tops it up in the background, and
 * pre-loads the images of the next few events so advancing is instant.
 *
 * Because reviewed events leave the server's "unreviewed" list, refills always request page 1
 * and the store de-duplicates by id (events already held locally are ignored).
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { getErrorMessage, getEventQueue, isRequestCancelled } from '../services/api';
import useReviewStore from '../store/reviewStore';

const PAGE_SIZE = 50;
const PRELOAD_AHEAD = 4; // images for the next 3-5 events are warmed up
const REFILL_THRESHOLD = 15; // fetch more when fewer than this many events remain after the current
const REFILL_COOLDOWN_MS = 1500;
const MAX_CACHED_IMAGES = 60;

/** Start loading `url` into the browser cache, keeping a reference so it is not GC'd mid-load. */
function preloadImage(cache, url) {
  if (!url || cache.has(url)) return;
  const image = new Image();
  image.decoding = 'async';
  image.src = url;
  cache.set(url, image);
  if (cache.size > MAX_CACHED_IMAGES) {
    cache.delete(cache.keys().next().value); // Map iterates oldest-first
  }
}

export default function useEventQueue({ pageSize = PAGE_SIZE, preloadAhead = PRELOAD_AHEAD } = {}) {
  const activeSiteId = useReviewStore((s) => s.activeSiteId);
  const activeFeature = useReviewStore((s) => s.activeFeature);
  const eventQueue = useReviewStore((s) => s.eventQueue);
  const currentIndex = useReviewStore((s) => s.currentIndex);
  const setQueue = useReviewStore((s) => s.setQueue);
  const appendEvents = useReviewStore((s) => s.appendEvents);

  const [status, setStatus] = useState('idle'); // idle | loading | ready | error
  const [error, setError] = useState(null);
  const [exhausted, setExhausted] = useState(false); // nothing more to fetch from the server
  const [isRefilling, setIsRefilling] = useState(false);
  const [refillError, setRefillError] = useState(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [retryTick, setRetryTick] = useState(0);

  const generationRef = useRef(0); // bumps on every (re)load so stale responses are ignored
  const refillingRef = useRef(false);
  const lastRefillRef = useRef(0);
  const imageCacheRef = useRef(new Map());

  const reload = useCallback(() => setReloadToken((n) => n + 1), []);

  // Re-enable background refills (e.g. after new events were imported) without resetting the queue.
  const refresh = useCallback(() => {
    setExhausted(false);
    setRetryTick((n) => n + 1);
  }, []);

  // ---- initial load (and reload) ------------------------------------------------------
  useEffect(() => {
    if (!activeSiteId || !activeFeature) {
      setStatus('idle');
      setError(null);
      return undefined;
    }

    generationRef.current += 1;
    refillingRef.current = false;
    setIsRefilling(false);
    setRefillError(null);
    setExhausted(false);
    setStatus('loading');
    setError(null);

    const controller = new AbortController();
    let cancelled = false;

    getEventQueue(activeSiteId, activeFeature, 1, pageSize, { signal: controller.signal })
      .then((data) => {
        if (cancelled) return;
        setQueue(data.items, data.total);
        setExhausted(!data.has_more);
        setStatus('ready');
      })
      .catch((err) => {
        if (cancelled || isRequestCancelled(err)) return;
        setError(getErrorMessage(err));
        setStatus('error');
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [activeSiteId, activeFeature, reloadToken, pageSize, setQueue]);

  // ---- background refill ----------------------------------------------------------------
  const remainingAfterCurrent = eventQueue.length - currentIndex - 1;

  useEffect(() => {
    if (status !== 'ready' || exhausted || refillingRef.current) return undefined;
    if (remainingAfterCurrent >= REFILL_THRESHOLD) return undefined;

    const wait = REFILL_COOLDOWN_MS - (Date.now() - lastRefillRef.current);
    if (wait > 0) {
      const timer = setTimeout(() => setRetryTick((n) => n + 1), wait);
      return () => clearTimeout(timer);
    }

    const generation = generationRef.current;
    refillingRef.current = true;
    lastRefillRef.current = Date.now();
    setIsRefilling(true);
    setRefillError(null);

    getEventQueue(activeSiteId, activeFeature, 1, pageSize)
      .then((data) => {
        if (generation !== generationRef.current) return; // site/feature changed meanwhile
        appendEvents(data.items, data.total);
        setExhausted(!data.has_more);
      })
      .catch((err) => {
        if (generation !== generationRef.current) return;
        setRefillError(getErrorMessage(err));
      })
      .finally(() => {
        if (generation !== generationRef.current) return;
        refillingRef.current = false;
        setIsRefilling(false);
      });

    return undefined;
  }, [status, exhausted, remainingAfterCurrent, retryTick, activeSiteId, activeFeature,
    pageSize, appendEvents]);

  // ---- image pre-loading ------------------------------------------------------------------
  useEffect(() => {
    const cache = imageCacheRef.current;
    for (let offset = 1; offset <= preloadAhead; offset += 1) {
      const event = eventQueue[currentIndex + offset];
      if (!event) break;
      preloadImage(cache, event.crop_image_url); // small, so it lands first
      preloadImage(cache, event.full_image_url);
    }
  }, [eventQueue, currentIndex, preloadAhead]);

  useEffect(() => {
    const cache = imageCacheRef.current;
    return () => cache.clear();
  }, []);

  return {
    status,
    error,
    isLoading: status === 'idle' || status === 'loading',
    isRefilling,
    refillError,
    exhausted,
    reload,
    refresh,
  };
}
