"""Celery application: Redis broker + result backend, task routing and the beat schedule.

Queues
------
ingestion    local-folder ingestion and hot-folder scans (disk / DB bound, can be large)
sftp         remote pulls (network bound, can be slow - kept apart so they never starve ingestion)
maintenance  housekeeping

Run (from backend/):
    celery -A app.core.celery_app:celery_app worker -Q ingestion,sftp,maintenance -l INFO
    celery -A app.core.celery_app:celery_app beat -l INFO        # exactly ONE beat process
"""

from __future__ import annotations

import re
from typing import Any

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init
from kombu import Queue

from app.core.config import get_settings

settings = get_settings()

# ---- task names (used by the decorators in app.tasks and by the routing table below)
TASK_INGEST_LOCAL = "aevaf.ingest_local_folder"
TASK_SFTP_SYNC = "aevaf.sftp_sync"
TASK_SCHEDULED_SFTP = "aevaf.scheduled_sftp_syncs"
TASK_SCAN_HOT_FOLDERS = "aevaf.scan_hot_folders"
TASK_CLEANUP = "aevaf.cleanup_database"
TASK_INGEST_UPLOAD = "aevaf.ingest_upload"

QUEUE_INGESTION = "ingestion"
QUEUE_SFTP = "sftp"
QUEUE_MAINTENANCE = "maintenance"


def _with_db(url: str, database: int) -> str:
    """Return a Redis URL pointing at logical database ``database``."""
    if re.search(r"/\d+$", url):
        return re.sub(r"/\d+$", f"/{database}", url)
    return f"{url.rstrip('/')}/{database}"


def _build_beat_schedule() -> dict[str, dict[str, Any]]:
    """Periodic jobs; entries are only added when the matching feature is configured."""
    schedule: dict[str, dict[str, Any]] = {}

    if settings.sftp_sync_interval_minutes > 0 and settings.sftp_sync_targets.strip():
        schedule["scheduled-sftp-sync"] = {
            "task": TASK_SCHEDULED_SFTP,
            "schedule": settings.sftp_sync_interval_minutes * 60.0,
        }
    if settings.watch_root is not None and settings.watch_scan_interval_minutes > 0:
        schedule["hot-folder-scan"] = {
            "task": TASK_SCAN_HOT_FOLDERS,
            "schedule": settings.watch_scan_interval_minutes * 60.0,
        }
    schedule["database-cleanup"] = {
        "task": TASK_CLEANUP,
        "schedule": crontab(hour=settings.cleanup_hour_utc, minute=15),
    }
    return schedule


BROKER_URL = settings.celery_broker_url or settings.redis_url
RESULT_BACKEND = settings.celery_result_backend or _with_db(settings.redis_url, 1)

celery_app = Celery(
    "aevaf",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=["app.tasks.ingestion_tasks"],
)

celery_app.conf.update(
    # serialisation: JSON only, never pickle
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # routing
    task_default_queue=QUEUE_INGESTION,
    task_queues=(Queue(QUEUE_INGESTION), Queue(QUEUE_SFTP), Queue(QUEUE_MAINTENANCE)),
    task_routes={
        TASK_INGEST_LOCAL: {"queue": QUEUE_INGESTION},
        TASK_INGEST_UPLOAD: {"queue": QUEUE_INGESTION},
        TASK_SCAN_HOT_FOLDERS: {"queue": QUEUE_INGESTION},
        TASK_SFTP_SYNC: {"queue": QUEUE_SFTP},
        TASK_SCHEDULED_SFTP: {"queue": QUEUE_SFTP},
        TASK_CLEANUP: {"queue": QUEUE_MAINTENANCE},
    },
    beat_schedule=_build_beat_schedule(),
    # reliability: a task is acknowledged only after it finishes, so a crashed worker's tasks
    # are redelivered. All tasks are idempotent (duplicates are skipped), so this is safe.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,   # long tasks: don't hoard work behind a busy process
    task_track_started=True,
    task_soft_time_limit=3300,
    task_time_limit=3600,
    result_expires=24 * 3600,
    broker_connection_retry_on_startup=True,
    # must exceed the longest task, or Redis redelivers a task that is still running
    broker_transport_options={"visibility_timeout": 2 * 3600},
)


@worker_process_init.connect
def _reset_database_pool(**_: object) -> None:
    """Forked worker processes must not share the parent's database connections."""
    from app.core.database import engine

    engine.dispose(close=False)
