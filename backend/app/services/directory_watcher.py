"""Hot-folder watcher: ingest event packages automatically as they arrive.

Folder convention (under ``WATCH_ROOT``)::

    <watch_root>/<site_code>/<feature>/...JSON + image files (sub-folders allowed)...

e.g. ``/hot/T1/seatbelt/C-100234.json``. The first two path components select the site (by its
``code``) and the feature. Unknown sites or features are ignored, never auto-created.

How it works
------------
* ``watchdog`` reports file changes. Temporary files (``.part`` ...) and hidden files are ignored.
* Changes are *debounced per folder*: once a folder has been quiet for ``WATCH_SETTLE_SECONDS``
  (so copies/uploads have finished) one Celery ingestion task is queued for it.
* Ingestion is idempotent - already-ingested packages are skipped - so repeated triggers are
  harmless, and nothing is ever deleted or moved in the hot folder.
* On start-up every non-empty ``<site>/<feature>`` folder is queued once, to catch up on files
  delivered while the watcher was down. A periodic Celery task (``scan_hot_folders``) does the
  same as a safety net for events inotify can miss (network shares, container mounts).

Run:  python -m app.services.directory_watcher
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from sqlalchemy import select
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.models import Site
from app.db.schemas import normalize_feature
from app.services.fetchers.base import SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)

IGNORED_SUFFIXES = (".part", ".tmp", ".temp", ".partial", ".crdownload", ".swp", "~")
HEARTBEAT_FILE = Path(os.environ.get("WATCHER_HEARTBEAT_FILE", "/tmp/directory_watcher.heartbeat"))
HEARTBEAT_SECONDS = 30.0
RETRY_SECONDS = 30.0


@dataclass(frozen=True)
class HotFolder:
    """One ``<site_code>/<feature>`` folder."""

    site_code: str
    feature: str
    path: Path


Dispatcher = Callable[[HotFolder], None]


# ------------------------------------------------------------------------- path helpers
def is_package_file(path: str | Path) -> bool:
    """True for finished-looking JSON/image files (not hidden, not a temp/partial file)."""
    name = Path(path).name.lower()
    if not name or name.startswith("."):
        return False
    if name.endswith(IGNORED_SUFFIXES):
        return False
    return Path(name).suffix in SUPPORTED_EXTENSIONS


def locate_hot_folder(root: Path, path: Path, *, is_directory: bool = False) -> HotFolder | None:
    """Map a changed path to its ``<site>/<feature>`` folder, or ``None`` if it is not inside one."""
    root_abs = Path(os.path.abspath(root))
    try:
        relative = Path(os.path.abspath(path)).relative_to(root_abs)
    except ValueError:
        return None  # outside the watched root

    parts = relative.parts
    if len(parts) < (2 if is_directory else 3):
        return None  # a file must sit at least one level below <site>/<feature>
    site_dir, feature_dir = parts[0], parts[1]
    if site_dir.startswith(".") or feature_dir.startswith("."):
        return None
    try:
        feature = normalize_feature(feature_dir)
    except ValueError:
        return None  # not a supported feature name
    return HotFolder(site_code=site_dir, feature=feature, path=root_abs / site_dir / feature_dir)


def discover_hot_folders(root: Path) -> list[HotFolder]:
    """Every ``<site>/<feature>`` folder under ``root`` that currently holds a package file."""
    found: list[HotFolder] = []
    if not root.is_dir():
        return found
    for site_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for feature_dir in sorted(p for p in site_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
            try:
                feature = normalize_feature(feature_dir.name)
            except ValueError:
                continue
            if any(f.is_file() and is_package_file(f) for f in feature_dir.rglob("*")):
                found.append(HotFolder(site_dir.name, feature, feature_dir))
    return found


# -------------------------------------------------------------------------- dispatching
def default_dispatch(folder: HotFolder) -> None:
    """Look up the site and queue a Celery ingestion task for the folder."""
    from app.tasks.ingestion_tasks import async_ingest_local_folder  # lazy: avoids an import cycle

    with SessionLocal() as db:
        site_id = db.scalar(select(Site.id).where(Site.code == folder.site_code))
    if site_id is None:
        logger.warning("Ignoring %s: no site with code %r exists", folder.path, folder.site_code)
        return
    async_ingest_local_folder.delay(str(folder.path), site_id, folder.feature)
    logger.info("Queued ingestion for %s (site %s, %s)", folder.path, site_id, folder.feature)


class Debouncer:
    """Collapses bursts of events per folder into one dispatch after a quiet period."""

    def __init__(self, settle_seconds: float, dispatch: Dispatcher,
                 retry_seconds: float = RETRY_SECONDS) -> None:
        self._settle = settle_seconds
        self._dispatch = dispatch
        self._retry = retry_seconds
        self._timers: dict[Path, threading.Timer] = {}
        self._lock = threading.Lock()
        self._closed = False

    def touch(self, folder: HotFolder, delay: float | None = None) -> None:
        """(Re)start the quiet-period timer for ``folder``."""
        with self._lock:
            if self._closed:
                return
            previous = self._timers.pop(folder.path, None)
            if previous is not None:
                previous.cancel()
            timer = threading.Timer(self._settle if delay is None else delay, self._fire, args=(folder,))
            timer.daemon = True
            self._timers[folder.path] = timer
            timer.start()

    def _fire(self, folder: HotFolder) -> None:
        with self._lock:
            if self._timers.get(folder.path) is threading.current_thread():
                del self._timers[folder.path]
        try:
            self._dispatch(folder)
        except Exception:  # noqa: BLE001 - e.g. broker temporarily down: try again later
            logger.exception("Dispatch failed for %s; retrying in %.0fs", folder.path, self._retry)
            self.touch(folder, delay=self._retry)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()


class HotFolderEventHandler(FileSystemEventHandler):
    """Translates watchdog events into debounced per-folder triggers."""

    def __init__(self, root: Path, debouncer: Debouncer) -> None:
        super().__init__()
        self._root = root
        self._debouncer = debouncer

    def _handle(self, raw_path: str | bytes, is_directory: bool) -> None:
        path = Path(os.fsdecode(raw_path))
        if not is_directory and not is_package_file(path):
            return
        folder = locate_hot_folder(self._root, path, is_directory=is_directory)
        if folder is not None:
            self._debouncer.touch(folder)

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(event.src_path, event.is_directory)  # a dropped-in directory also counts

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path, False)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Atomic "write to temp name, then rename" deliveries arrive as a move.
        self._handle(event.dest_path, event.is_directory)

    def on_closed(self, event: FileSystemEvent) -> None:  # inotify: writer closed the file
        if not event.is_directory:
            self._handle(event.src_path, False)


class DirectoryWatcher:
    """Owns the watchdog observer and the debouncer."""

    def __init__(
        self,
        root: Path,
        *,
        settle_seconds: float = 3.0,
        use_polling: bool = False,
        poll_interval: float = 5.0,
        dispatch: Dispatcher = default_dispatch,
    ) -> None:
        self.root = Path(os.path.abspath(root))
        self._debouncer = Debouncer(settle_seconds, dispatch)
        self._observer = PollingObserver(timeout=poll_interval) if use_polling else Observer()
        self._observer.schedule(
            HotFolderEventHandler(self.root, self._debouncer), str(self.root), recursive=True
        )

    def start(self) -> None:
        self._observer.start()
        self.catch_up()  # after the observer is live, so nothing falls in the gap

    def catch_up(self) -> None:
        """Queue every folder that already holds packages."""
        folders = discover_hot_folders(self.root)
        logger.info("Catch-up scan found %d folder(s) with packages", len(folders))
        for folder in folders:
            self._debouncer.touch(folder)

    def is_alive(self) -> bool:
        return self._observer.is_alive()

    def stop(self) -> None:
        self._debouncer.close()
        self._observer.stop()
        self._observer.join(timeout=5)


# --------------------------------------------------------------------------- entry point
def _touch_heartbeat() -> None:
    try:
        HEARTBEAT_FILE.touch()  # the container healthcheck looks at this file's age
    except OSError:
        logger.debug("Could not update heartbeat file", exc_info=True)


def main() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    root = settings.watch_root
    if root is None:
        logger.error("WATCH_ROOT is not set; there is nothing to watch")
        return 1
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.error("Watch folder %s does not exist and cannot be created", root)
        return 1

    watcher = DirectoryWatcher(
        root,
        settle_seconds=settings.watch_settle_seconds,
        use_polling=settings.watch_use_polling,
        poll_interval=settings.watch_poll_interval_seconds,
    )

    stop_event = threading.Event()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    watcher.start()
    logger.info(
        "Watching %s (%s, settle %.1fs)",
        root, "polling" if settings.watch_use_polling else "inotify", settings.watch_settle_seconds,
    )
    _touch_heartbeat()

    exit_code = 0
    while not stop_event.wait(HEARTBEAT_SECONDS):
        if not watcher.is_alive():
            logger.error("The file observer stopped unexpectedly; exiting so Docker restarts us")
            exit_code = 1
            break
        _touch_heartbeat()

    watcher.stop()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
