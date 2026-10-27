"""Celery tasks: background ingestion, scheduled SFTP syncs, hot-folder scans, housekeeping.

Every task is idempotent (files already copied and events already stored are skipped), which is
what makes ``acks_late`` redelivery and repeated triggers safe.
"""

from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timedelta
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import redis
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from app.core.celery_app import (
    TASK_CLEANUP,
    TASK_INGEST_LOCAL,
    TASK_INGEST_UPLOAD,
    TASK_SCAN_HOT_FOLDERS,
    TASK_SCHEDULED_SFTP,
    TASK_SFTP_SYNC,
    celery_app,
)
from app.core.config import Settings, get_settings
from app.core.database import SessionLocal
from app.db.models import ImportJob, Site
from app.db.schemas import normalize_feature
from app.services.directory_watcher import discover_hot_folders
from app.services.fetchers.base import FetcherConfigError, FetcherConnectionError, FetchResult
from app.services.fetchers.local_fetcher import LocalFetcher
from app.services.fetchers.sftp_fetcher import SftpConfig, SftpFetcher
from app.services.parser import PackageParser
from app.services.smart_ingest import UploadIngestor

logger = logging.getLogger(__name__)

LOCK_TTL_SECONDS = 1800


class FolderBusyError(Exception):
    """Another worker is already ingesting this source; the task is retried shortly."""


# ----------------------------------------------------------------------------- helpers
@contextmanager
def _exclusive(name: str, ttl: int = LOCK_TTL_SECONDS) -> Iterator[None]:
    """Redis lock so two workers never ingest the same source at the same time.

    Fails open: if Redis cannot be reached the work simply runs without the lock (ingestion is
    idempotent, so the worst case is some duplicated effort).
    """
    client = None
    lock = None
    busy = False
    try:
        client = redis.Redis.from_url(str(celery_app.conf.broker_url))
        candidate = client.lock(f"aevaf:lock:{name}", timeout=ttl)
        if candidate.acquire(blocking=False):
            lock = candidate
        else:
            busy = True
    except redis.exceptions.RedisError:
        logger.warning("Redis unavailable; continuing without a lock for %s", name)

    if busy:
        if client is not None:
            client.close()
        raise FolderBusyError(name)
    try:
        yield
    finally:
        if lock is not None:
            try:
                lock.release()
            except redis.exceptions.RedisError:
                logger.warning("Lock %s was not released cleanly", name)
        if client is not None:
            client.close()

def _summarise(kind: str, site_id: int, feature: str, source: str,
               fetched: FetchResult, parse_stats: Any) -> dict[str, Any]:
    """JSON-serialisable task result (stored in the Redis result backend)."""
    return {
        "kind": kind,
        "site_id": site_id,
        "feature": feature,
        "source": source,
        "transfer": fetched.to_stats().model_dump(),
        "parse": parse_stats.model_dump(),
    }


def _require_allowed_local_folder(folder_path: str, settings: Settings) -> Path:
    """Same rule as the API: only folders under LOCAL_INGEST_ROOTS or WATCH_ROOT.

    With neither configured, any folder is accepted in development and none in other environments.
    """
    folder = Path(folder_path).resolve()
    if not folder.is_dir():
        raise FetcherConfigError(f"Not a directory: {folder_path}")

    roots = [root.resolve() for root in settings.local_ingest_roots]
    if settings.watch_root is not None:
        roots.append(settings.watch_root.resolve())
    if not roots:
        if settings.environment == "development":
            return folder
        raise PermissionError("Local ingestion is disabled: configure LOCAL_INGEST_ROOTS or WATCH_ROOT")
    if not any(folder.is_relative_to(root) for root in roots):
        raise PermissionError(f"{folder} is outside the allowed ingestion roots")
    return folder


def parse_sftp_targets(raw: str) -> list[tuple[int, str]]:
    """Parse ``"1:seatbelt, 2:alpr"`` into ``[(1, 'seatbelt'), (2, 'alpr')]``; bad entries are skipped."""
    targets: list[tuple[int, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        site_part, _, feature_part = item.partition(":")
        try:
            targets.append((int(site_part), normalize_feature(feature_part)))
        except ValueError:
            logger.warning("Ignoring invalid SFTP_SYNC_TARGETS entry %r", item)
    return targets


def _sftp_config_for(site_id: int, feature: str, settings: Settings) -> SftpConfig:
    """Build the connection settings for one site/feature from the SFTP_* environment settings."""
    with SessionLocal() as db:
        site = db.get(Site, site_id)
    if site is None:
        raise ValueError(f"Site {site_id} does not exist")

    if not settings.sftp_default_host:
        raise FetcherConfigError("SFTP_DEFAULT_HOST is not configured")
    username = settings.sftp_default_username
    password = settings.sftp_default_password.get_secret_value() if settings.sftp_default_password else None
    key_path = str(settings.sftp_default_key_path) if settings.sftp_default_key_path else None
    if not username or not (password or key_path):
        raise FetcherConfigError("Set SFTP_DEFAULT_USERNAME and a password or SFTP_DEFAULT_KEY_PATH")

    try:
        remote_path = settings.sftp_remote_path_template.format(
            site_code=site.code, site_id=site.id, feature=feature
        )
    except (KeyError, IndexError, ValueError) as exc:
        raise FetcherConfigError(
            "SFTP_REMOTE_PATH_TEMPLATE may only use {site_code}, {site_id} and {feature}"
        ) from exc

    return SftpConfig(
        host=settings.sftp_default_host,
        port=settings.sftp_default_port,
        username=username,
        password=password,
        private_key_path=key_path,
        remote_path=remote_path,
        recursive=True,
    )


# ------------------------------------------------------------------------------- tasks
@celery_app.task(
    bind=True,
    name=TASK_INGEST_LOCAL,
    autoretry_for=(OperationalError, FolderBusyError),
    retry_backoff=15,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=8,
)
def async_ingest_local_folder(self: Any, folder_path: str, site_id: int, feature: str) -> dict[str, Any]:
    """Copy and ingest every package under ``folder_path`` for a site/feature.

    Intended for large directories and for the hot-folder watcher. Retries automatically when
    the database is briefly unavailable or another worker holds the folder.
    """
    settings = get_settings()
    feature = normalize_feature(feature)
    folder = _require_allowed_local_folder(folder_path, settings)

    with _exclusive(f"local:{folder}"):
        fetched = LocalFetcher(site_id, feature, folder, settings=settings).fetch()
        with SessionLocal() as db:
            stats = PackageParser(db, settings=settings).ingest_fetch_result(fetched)

    logger.info(
        "Local ingestion of %s: %d new events, %d duplicates, %d invalid",
        folder, stats.events_created, stats.duplicates_skipped, stats.invalid_records,
    )
    return _summarise("local", site_id, feature, str(folder), fetched, stats)


@celery_app.task(
    bind=True,
    name=TASK_SFTP_SYNC,
    autoretry_for=(FetcherConnectionError, OperationalError, FolderBusyError),
    retry_backoff=30,
    retry_backoff_max=900,
    retry_jitter=True,
    max_retries=3,
)
def async_sftp_sync(self: Any, site_id: int, feature: str) -> dict[str, Any]:
    """Pull new packages for a site/feature from the configured SFTP server and ingest them.

    The server and credentials come from the ``SFTP_DEFAULT_*`` settings and the remote folder
    from ``SFTP_REMOTE_PATH_TEMPLATE`` (default ``/{site_code}/{feature}``).
    """
    settings = get_settings()
    feature = normalize_feature(feature)
    config = _sftp_config_for(site_id, feature, settings)

    with _exclusive(f"sftp:{site_id}:{feature}"):
        with SftpFetcher(site_id, feature, config, settings=settings) as fetcher:
            fetched = fetcher.fetch()
        with SessionLocal() as db:
            stats = PackageParser(db, settings=settings).ingest_fetch_result(fetched)

    logger.info(
        "SFTP sync site %s/%s: %d downloaded, %d new events",
        site_id, feature, fetched.downloaded, stats.events_created,
    )
    return _summarise("sftp", site_id, feature, config.remote_path, fetched, stats)


@celery_app.task(name=TASK_SCHEDULED_SFTP)
def scheduled_sftp_syncs() -> dict[str, Any]:
    """Beat entry point: queue one ``async_sftp_sync`` per entry of ``SFTP_SYNC_TARGETS``."""
    targets = parse_sftp_targets(get_settings().sftp_sync_targets)
    for site_id, feature in targets:
        async_sftp_sync.delay(site_id, feature)
    return {"queued": [f"{site_id}:{feature}" for site_id, feature in targets]}


@celery_app.task(name=TASK_SCAN_HOT_FOLDERS)
def scan_hot_folders() -> dict[str, Any]:
    """Safety-net scan of ``WATCH_ROOT``: queue ingestion for every non-empty site/feature folder."""
    settings = get_settings()
    if settings.watch_root is None:
        return {"queued": [], "note": "WATCH_ROOT is not set"}

    with SessionLocal() as db:
        site_ids = {code: site_id for site_id, code in db.execute(select(Site.id, Site.code))}

    queued: list[str] = []
    for folder in discover_hot_folders(settings.watch_root):
        site_id = site_ids.get(folder.site_code)
        if site_id is None:
            logger.warning("Hot folder %s has no matching site code", folder.path)
            continue
        async_ingest_local_folder.delay(str(folder.path), site_id, folder.feature)
        queued.append(str(folder.path))
    return {"queued": queued}


def _remove_stale_partial_files(root: Path, max_age_hours: float = 24.0) -> int:
    """Delete ``*.part`` temp files left behind by interrupted transfers."""
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for path in root.rglob("*.part"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            logger.warning("Could not remove stale temp file %s", path)
    return removed


def _analyze_tables() -> bool:
    """Refresh PostgreSQL planner statistics so analytics queries stay fast after bulk ingests."""
    with SessionLocal() as db:
        if db.get_bind().dialect.name != "postgresql":
            return False
        db.execute(text("ANALYZE events"))
        db.execute(text("ANALYZE event_reviews"))
        db.commit()
    return True


@celery_app.task(name=TASK_CLEANUP)
def cleanup_database() -> dict[str, Any]:
    """Nightly housekeeping. Deliberately non-destructive: no events or reviews are deleted."""
    settings = get_settings()
    removed = _remove_stale_partial_files(settings.events_dir)
    stale_uploads = _remove_stale_upload_folders(settings.uploads_dir)
    abandoned = _abandon_stale_imports()
    analyzed = _analyze_tables()
    logger.info(
        "Cleanup: %d temp files, %d stale upload folders, %d abandoned imports, analysed: %s",
        removed, stale_uploads, abandoned, analyzed,
    )
    return {
        "stale_partial_files_removed": removed,
        "stale_upload_folders_removed": stale_uploads,
        "abandoned_imports_closed": abandoned,
        "tables_analyzed": analyzed,
    }


# ------------------------------------------------------------------ browser-upload imports
def _finish_import(import_id: str, **fields: Any) -> None:
    """Record the outcome of an import on its row (always stamps ``finished_at``)."""
    with SessionLocal() as db:
        job = db.get(ImportJob, import_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)
        job.finished_at = datetime.utcnow()
        db.commit()


@celery_app.task(bind=True, name=TASK_INGEST_UPLOAD, max_retries=5)
def async_ingest_upload(self: Any, import_id: str) -> dict[str, Any]:
    """Ingest the files a user uploaded through the Import page, then delete the staging folder.

    JSON data files and full images (usually from two different folders) are paired up by the
    capture time / track id / plate in their names; images without a data file still become
    events. See app.services.smart_ingest.
    """
    settings = get_settings()

    with SessionLocal() as db:
        job = db.get(ImportJob, import_id)
        if job is None:
            raise ValueError(f"Import {import_id} does not exist")
        if job.status in ("completed", "failed"):  # redelivered after a worker crash
            return {"import_id": import_id, "status": job.status}
        job.status = "processing"
        db.commit()
        site_id, feature, timezone = job.site_id, job.feature, job.timezone

    folder = settings.uploads_dir / import_id
    try:
        with _exclusive(f"upload:{import_id}"):
            with SessionLocal() as db:
                outcome = UploadIngestor(db, settings, timezone).run(folder, site_id, feature)
    except (OperationalError, FolderBusyError) as exc:
        if self.request.retries >= self.max_retries:
            _finish_import(import_id, status="failed",
                           error=f"Gave up after repeated errors ({exc.__class__.__name__})")
            raise
        raise self.retry(exc=exc, countdown=min(30 * 2 ** self.request.retries, 300))
    except Exception as exc:
        logger.exception("Upload ingestion %s failed", import_id)
        _finish_import(import_id, status="failed", error=str(exc)[:500])
        raise

    _finish_import(
        import_id,
        status="completed",
        events_created=outcome.created,
        events_from_json=outcome.from_json,
        events_images_only=outcome.images_only,
        duplicates_skipped=outcome.duplicates,
        invalid_records=outcome.invalid,
        issues=outcome.issues[:10] or None,
        error=None,
    )
    shutil.rmtree(folder, ignore_errors=True)
    logger.info("Import %s done: %d new (%d images only), %d duplicates, %d invalid", import_id,
                outcome.created, outcome.images_only, outcome.duplicates, outcome.invalid)
    return {
        "import_id": import_id,
        "status": "completed",
        "events_created": outcome.created,
        "events_images_only": outcome.images_only,
        "duplicates_skipped": outcome.duplicates,
        "invalid_records": outcome.invalid,
    }


def _remove_stale_upload_folders(root: Path, max_age_hours: float = 72.0) -> int:
    """Delete staging folders of failed or abandoned uploads."""
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for folder in root.iterdir():
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        except OSError:
            logger.warning("Could not inspect upload folder %s", folder)
    return removed


def _abandon_stale_imports(max_age_hours: float = 24.0) -> int:
    """Close upload sessions that were started but never finished."""
    cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
    with SessionLocal() as db:
        result = db.execute(
            update(ImportJob)
            .where(ImportJob.status == "uploading", ImportJob.created_at < cutoff)
            .values(status="failed", error="Upload abandoned", finished_at=datetime.utcnow())
        )
        db.commit()
        return int(result.rowcount or 0)
