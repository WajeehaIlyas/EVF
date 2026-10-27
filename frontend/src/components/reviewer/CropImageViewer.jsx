import { useEffect, useRef, useState } from 'react';

const clampPercent = (value) => Math.min(100, Math.max(0, value));

/** One titled image tile with its own loading / error overlay. Remount with `key` per source. */
function ImagePanel({ title, src, alt, onMove, onLeave }) {
  const imgRef = useRef(null);
  const [status, setStatus] = useState(src ? 'loading' : 'empty'); // loading | loaded | error | empty

  useEffect(() => {
    const img = imgRef.current;
    if (img && img.complete && img.naturalWidth > 0) setStatus('loaded'); // served from cache
  }, []);

  return (
    <figure className="relative flex h-44 items-center justify-center overflow-hidden rounded-lg bg-slate-900 sm:h-52">
      <figcaption className="absolute left-2 top-2 z-10 rounded bg-black/60 px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide text-slate-200">
        {title}
      </figcaption>

      {src && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          ref={imgRef}
          src={src}
          alt={alt}
          draggable={false}
          onLoad={() => setStatus('loaded')}
          onError={() => setStatus('error')}
          onMouseMove={onMove}
          onMouseLeave={onLeave}
          className={`max-h-full max-w-full select-none object-contain ${onMove ? 'cursor-crosshair' : ''}`}
          style={{ opacity: status === 'loaded' ? 1 : 0 }}
        />
      )}

      {status === 'loading' && (
        <div className="absolute inset-0 flex items-center justify-center bg-slate-900/70" role="status">
          <div className="h-7 w-7 animate-spin rounded-full border-4 border-slate-600 border-t-indigo-400" />
          <span className="sr-only">Loading image…</span>
        </div>
      )}
      {(status === 'error' || status === 'empty') && (
        <p className="absolute inset-0 flex items-center justify-center px-4 text-center text-xs text-slate-400">
          {status === 'empty' ? 'No image available' : 'Image failed to load'}
        </p>
      )}
    </figure>
  );
}

/**
 * Vehicle crop plus a second tile for close inspection:
 *  - if `plateUrl` is supplied, the plate crop is shown there;
 *  - otherwise the second tile is a 3x magnifier that follows the cursor over the crop.
 * (The current backend stores a single crop per event, so the magnifier is the default.)
 */
export default function CropImageViewer({ cropUrl, plateUrl, alt = 'Vehicle crop' }) {
  const [lens, setLens] = useState({ x: 50, y: 50 });

  const handleMove = (event) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    setLens({
      x: clampPercent(((event.clientX - rect.left) / rect.width) * 100),
      y: clampPercent(((event.clientY - rect.top) / rect.height) * 100),
    });
  };

  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      <ImagePanel
        key={cropUrl || 'no-crop'}
        title="Vehicle crop"
        src={cropUrl}
        alt={alt}
        onMove={plateUrl ? undefined : handleMove}
        onLeave={plateUrl ? undefined : () => setLens({ x: 50, y: 50 })}
      />

      {plateUrl ? (
        <ImagePanel key={plateUrl} title="Plate crop" src={plateUrl} alt="Number plate crop" />
      ) : (
        <figure
          className="relative h-44 overflow-hidden rounded-lg bg-slate-900 sm:h-52"
          aria-label="Magnified detail of the vehicle crop"
        >
          <figcaption className="absolute left-2 top-2 z-10 rounded bg-black/60 px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide text-slate-200">
            Detail 3× · hover the crop
          </figcaption>
          {cropUrl ? (
            <div
              className="h-full w-full"
              style={{
                backgroundImage: `url("${cropUrl}")`,
                backgroundRepeat: 'no-repeat',
                backgroundSize: '300%',
                backgroundPosition: `${lens.x}% ${lens.y}%`,
              }}
            />
          ) : (
            <p className="flex h-full items-center justify-center text-xs text-slate-400">
              No image available
            </p>
          )}
        </figure>
      )}
    </div>
  );
}
