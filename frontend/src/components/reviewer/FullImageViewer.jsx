import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

const MIN_SCALE = 1;
const MAX_SCALE = 8;
const BUTTON_STEP = 1.4;
const IDENTITY = { scale: 1, x: 0, y: 0 };
const LENS_SIZE = 240; // px, square lens shown on the image
const LENS_ZOOM = 3; // magnification relative to what is currently displayed

const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

/**
 * Hovering the image shows a 3x magnifier lens right on it, following the cursor (toggle with the
 * Magnifier button). Full-frame (databar) image with wheel-zoom toward the cursor, drag-to-pan, double-click zoom,
 * a full-screen toggle and a loading overlay. Keyboard hotkeys keep working in full screen.
 */
export default function FullImageViewer({ src, alt = 'Full event image', title = 'Full image' }) {
  const viewerRef = useRef(null); // outer element: the full-screen target
  const stageRef = useRef(null); // pan/zoom surface
  const imgRef = useRef(null);
  const dragRef = useRef(null);

  const [view, setView] = useState(IDENTITY);
  const [status, setStatus] = useState(src ? 'loading' : 'error'); // loading | loaded | error
  const [attempt, setAttempt] = useState(0);
  const [isDragging, setIsDragging] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [lensOn, setLensOn] = useState(true);
  const [lens, setLens] = useState(null); // { left, top, bgW, bgH, bgX, bgY } while hovering the image

  // New image (or retry): reset zoom and work out whether it is already cached.
  useLayoutEffect(() => {
    setView(IDENTITY);
    setIsDragging(false);
    setLens(null);
    dragRef.current = null;
    const img = imgRef.current;
    if (!src) setStatus('error');
    else if (img && img.complete && img.naturalWidth > 0) setStatus('loaded');
    else setStatus('loading');
  }, [src, attempt]);

  /** Zoom by `factor`, keeping the point under the cursor fixed (centre when no point given). */
  const zoomAt = useCallback((factor, point) => {
    setView((prev) => {
      const scale = clamp(prev.scale * factor, MIN_SCALE, MAX_SCALE);
      if (scale === prev.scale) return prev;
      if (scale <= MIN_SCALE) return IDENTITY;

      const rect = stageRef.current?.getBoundingClientRect();
      const px = rect && point ? point.x - rect.left - rect.width / 2 : 0;
      const py = rect && point ? point.y - rect.top - rect.height / 2 : 0;
      const ratio = scale / prev.scale;
      return { scale, x: px - ratio * (px - prev.x), y: py - ratio * (py - prev.y) };
    });
  }, []);

  // Wheel listener must be non-passive so we can stop the page from scrolling.
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return undefined;
    const onWheel = (event) => {
      event.preventDefault();
      zoomAt(Math.exp(-event.deltaY * 0.002), { x: event.clientX, y: event.clientY });
    };
    stage.addEventListener('wheel', onWheel, { passive: false });
    return () => stage.removeEventListener('wheel', onWheel);
  }, [zoomAt]);

  // Track full-screen state (also changes when the user presses Esc).
  useEffect(() => {
    const onChange = () => setIsFullscreen(document.fullscreenElement === viewerRef.current);
    document.addEventListener('fullscreenchange', onChange);
    return () => document.removeEventListener('fullscreenchange', onChange);
  }, []);

  const toggleFullscreen = useCallback(() => {
    const element = viewerRef.current;
    if (!element) return;
    if (document.fullscreenElement) document.exitFullscreen?.();
    else Promise.resolve(element.requestFullscreen?.()).catch(() => {});
  }, []);

  // ---- drag to pan ----------------------------------------------------------------------
  const onPointerDown = (event) => {
    if (event.button !== 0 || view.scale <= MIN_SCALE) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = { x: event.clientX, y: event.clientY };
    setIsDragging(true);
  };

  const updateLens = (event) => {
    const img = imgRef.current;
    const stage = stageRef.current;
    if (!lensOn || !img || !stage || status !== 'loaded') { setLens(null); return; }
    const box = img.getBoundingClientRect(); // includes the current zoom / pan
    const area = stage.getBoundingClientRect();
    const rx = event.clientX - box.left;
    const ry = event.clientY - box.top;
    if (rx < 0 || ry < 0 || rx > box.width || ry > box.height) { setLens(null); return; }
    setLens({
      left: event.clientX - area.left - LENS_SIZE / 2,
      top: event.clientY - area.top - LENS_SIZE / 2,
      bgW: box.width * LENS_ZOOM,
      bgH: box.height * LENS_ZOOM,
      bgX: LENS_SIZE / 2 - rx * LENS_ZOOM,
      bgY: LENS_SIZE / 2 - ry * LENS_ZOOM,
    });
  };

  const onPointerMove = (event) => {
    const drag = dragRef.current;
    if (!drag) { updateLens(event); return; }
    setLens(null);
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    dragRef.current = { x: event.clientX, y: event.clientY };
    const rect = stageRef.current?.getBoundingClientRect();
    setView((prev) => {
      const limitX = rect ? (rect.width * prev.scale) / 2 : Infinity;
      const limitY = rect ? (rect.height * prev.scale) / 2 : Infinity;
      return {
        ...prev,
        x: clamp(prev.x + dx, -limitX, limitX),
        y: clamp(prev.y + dy, -limitY, limitY),
      };
    });
  };

  const endDrag = (event) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    setIsDragging(false);
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const onDoubleClick = (event) => {
    if (view.scale > MIN_SCALE) setView(IDENTITY);
    else zoomAt(3, { x: event.clientX, y: event.clientY });
  };

  const cursor = view.scale > MIN_SCALE ? (isDragging ? 'cursor-grabbing' : 'cursor-grab') : (lensOn ? 'cursor-crosshair' : 'cursor-zoom-in');
  const buttonClass =
    'rounded-md bg-slate-800/80 px-2.5 py-1 text-xs font-medium text-slate-100 transition ' +
    'hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400';
  const keepFocus = (event) => event.preventDefault(); // clicking must not steal hotkey focus

  return (
    <div
      ref={viewerRef}
      className="flex h-full min-h-[280px] flex-col overflow-hidden rounded-xl border border-slate-300 bg-slate-950 shadow-sm dark:border-slate-700"
    >
      {/* toolbar */}
      <div className="flex items-center justify-between gap-2 bg-slate-900 px-3 py-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-300">{title}</span>
        <div className="flex items-center gap-1.5">
          <button type="button" className={buttonClass} onMouseDown={keepFocus}
            onClick={() => zoomAt(1 / BUTTON_STEP)} aria-label="Zoom out">−</button>
          <span className="w-12 text-center text-xs tabular-nums text-slate-300">
            {Math.round(view.scale * 100)}%
          </span>
          <button type="button" className={buttonClass} onMouseDown={keepFocus}
            onClick={() => zoomAt(BUTTON_STEP)} aria-label="Zoom in">+</button>
          <button type="button" className={buttonClass} onMouseDown={keepFocus}
            onClick={() => setView(IDENTITY)}>Fit</button>
          <button type="button" className={buttonClass} onMouseDown={keepFocus}
            onClick={toggleFullscreen}>{isFullscreen ? 'Exit full screen' : 'Full screen'}</button>
          <button type="button" className={`${buttonClass} ${lensOn ? 'ring-1 ring-indigo-400' : 'opacity-70'}`}
            onMouseDown={keepFocus} aria-pressed={lensOn}
            onClick={() => { setLensOn((v) => !v); setLens(null); }}>Magnifier</button>
        </div>
      </div>

      {/* pan / zoom surface */}
      <div
        ref={stageRef}
        className={`relative flex min-h-0 flex-1 touch-none select-none items-center justify-center overflow-hidden ${cursor}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onPointerLeave={() => setLens(null)}
        onDoubleClick={onDoubleClick}
      >
        {src && (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            key={`${src}#${attempt}`}
            ref={imgRef}
            src={src}
            alt={alt}
            draggable={false}
            onLoad={() => setStatus('loaded')}
            onError={() => setStatus('error')}
            className="max-h-full max-w-full select-none object-contain will-change-transform"
            style={{
              transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
              transition: isDragging ? 'none' : 'transform 80ms ease-out',
              opacity: status === 'loaded' ? 1 : 0,
            }}
          />
        )}

        {status === 'loading' && (
          <div className="absolute inset-0 flex items-center justify-center bg-slate-950/70" role="status">
            <div className="h-9 w-9 animate-spin rounded-full border-4 border-slate-600 border-t-indigo-400" />
            <span className="sr-only">Loading image…</span>
          </div>
        )}

        {status === 'error' && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-slate-950 text-center">
            <p className="text-sm text-slate-300">
              {src ? 'This image failed to load.' : 'No image available for this event.'}
            </p>
            {src && (
              <button type="button" className={buttonClass} onMouseDown={keepFocus}
                onClick={() => setAttempt((n) => n + 1)}>Retry</button>
            )}
          </div>
        )}

        {lens && src && (
          <div
            aria-hidden="true"
            className="pointer-events-none absolute z-10 rounded-full border-2 border-white/80 shadow-2xl ring-1 ring-black/40"
            style={{
              width: LENS_SIZE,
              height: LENS_SIZE,
              left: lens.left,
              top: lens.top,
              backgroundColor: '#020617',
              backgroundImage: `url("${src}")`,
              backgroundRepeat: 'no-repeat',
              backgroundSize: `${lens.bgW}px ${lens.bgH}px`,
              backgroundPosition: `${lens.bgX}px ${lens.bgY}px`,
            }}
          />
        )}

        {isFullscreen && (
          <p className="pointer-events-none absolute right-3 top-3 rounded bg-black/60 px-2 py-1 text-xs text-slate-200">
            Hotkeys still work · Esc to exit
          </p>
        )}
      </div>
    </div>
  );
}
