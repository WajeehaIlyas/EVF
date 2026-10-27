"""Ingestion endpoints: local-folder (synchronous) and SFTP (background job + WebSocket)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.ws.progress import ProgressReporter, job_registry
from app.core.config import Settings, get_settings
from app.core.database import SessionLocal, get_db
from app.db.models import Site
from app.db.schemas import IngestionResponse, IngestJobAccepted, normalize_feature
from app.services.fetchers.base import FetcherConfigError, FetcherError
from app.services.fetchers.local_fetcher import LocalFetcher
from app.services.fetchers.sftp_fetcher import SftpConfig, SftpFetcher
from app.services.parser import PackageParser

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ingest", tags=["ingestion"])

DbSession = Annotated[Session, Depends(get_db)]


# -------------------------------------------------------------------------- request models
class _FeatureMixin(BaseModel):
    site_id: int = Field(ge=1)
    feature: str

    @field_validator("feature")
    @classmethod
    def _normalise_feature(cls, value: str) -> str:
        return normalize_feature(value)


class LocalIngestRequest(_FeatureMixin):
    folder_path: str = Field(min_length=1)
    recursive: bool = True


class SftpIngestRequest(_FeatureMixin):
    host: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    username: str | None = None          # falls back to SFTP_DEFAULT_USERNAME
    password: SecretStr | None = None    # never logged or echoed (SecretStr)
    key_path: str | None = None          # private-key file path on the *server*
    remote_directory: str = Field(min_length=1)
    recursive: bool = True


# ----------------------------------------------------------------------------------- helpers
def _require_site(db: Session, site_id: int) -> None:
    if db.get(Site, site_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Site {site_id} not found")


def _check_local_path_allowed(folder: Path, settings: Settings) -> None:
    """Stop the API being used to read arbitrary server directories into /media."""
    roots = [root.resolve() for root in settings.local_ingest_roots]
    if not roots:
        if settings.environment == "development":
            return
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Local ingestion is disabled; set LOCAL_INGEST_ROOTS to allow specific folders",
        )
    if not any(folder.is_relative_to(root) for root in roots):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Folder is outside the allowed ingestion roots"
        )


# ------------------------------------------------------------------------------------ local
@router.post("/local", response_model=IngestionResponse, summary="Ingest a local folder")
def ingest_local(payload: LocalIngestRequest, db: DbSession) -> IngestionResponse:
    """Copy + parse a folder that exists on the server. Blocks until finished."""
    settings = get_settings()
    _require_site(db, payload.site_id)

    folder = Path(payload.folder_path).resolve()
    if not folder.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a directory: {payload.folder_path}")
    _check_local_path_allowed(folder, settings)

    try:
        fetcher = LocalFetcher(
            payload.site_id, payload.feature, folder, recursive=payload.recursive
        )
        result = fetcher.fetch()
        stats = PackageParser(db).ingest_fetch_result(result)
    except (FetcherConfigError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except FetcherError as exc:
        logger.exception("Local ingestion failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("Database error during local ingestion")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Database error during ingestion"
        ) from exc

    return IngestionResponse(transfer=result.to_stats(), parse=stats)


# -------------------------------------------------------------------------------------- SFTP
def _run_sftp_ingestion(job_id: str, site_id: int, feature: str, config: SftpConfig) -> None:
    """Background job (runs in a worker thread): download, parse, report progress."""
    reporter = ProgressReporter(job_id)
    reporter.running()
    try:
        with SftpFetcher(
            site_id, feature, config, progress_callback=reporter.on_transfer
        ) as fetcher:
            result = fetcher.fetch()

        reporter.parsing()
        with SessionLocal() as db:  # the request's session is gone; open our own
            stats = PackageParser(db).ingest_fetch_result(result)

        reporter.complete(result.to_stats(), stats)
    except (FetcherError, ValueError) as exc:
        logger.warning("SFTP job %s failed: %s", job_id, exc)
        reporter.fail(str(exc))
    except Exception:  # noqa: BLE001 - a background task must never die silently
        logger.exception("SFTP job %s crashed", job_id)
        reporter.fail("Unexpected error during ingestion; see server logs")


@router.post(
    "/sftp",
    response_model=IngestJobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start an SFTP ingestion job",
)
def ingest_sftp(
    payload: SftpIngestRequest, background_tasks: BackgroundTasks, db: DbSession
) -> IngestJobAccepted:
    """Queue a background download and return a ``job_id``.

    Connect to ``/ws/ingest-progress/{job_id}`` for live progress. Credentials fall back to
    the ``SFTP_DEFAULT_*`` settings only when the request supplies neither password nor key.
    """
    settings = get_settings()
    _require_site(db, payload.site_id)

    username = payload.username or settings.sftp_default_username
    if payload.password is None and payload.key_path is None:
        password = (
            settings.sftp_default_password.get_secret_value()
            if settings.sftp_default_password
            else None
        )
        key_path = str(settings.sftp_default_key_path) if settings.sftp_default_key_path else None
    else:
        password = payload.password.get_secret_value() if payload.password else None
        key_path = payload.key_path

    if not username:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "An SFTP username is required")
    if not (password or key_path):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide a password or key_path")
    if key_path and not Path(key_path).expanduser().is_file():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "key_path does not exist on the server")

    config = SftpConfig(
        host=payload.host,
        port=payload.port,
        username=username,
        password=password,
        private_key_path=str(Path(key_path).expanduser()) if key_path else None,
        remote_path=payload.remote_directory,
        recursive=payload.recursive,
    )

    state = job_registry.create()
    background_tasks.add_task(_run_sftp_ingestion, state.job_id, payload.site_id,
                              payload.feature, config)
    return IngestJobAccepted(job_id=state.job_id, ws_path=f"/ws/ingest-progress/{state.job_id}")


# ------------------------------------------------------------------ Celery-backed (queued)
class TaskAccepted(BaseModel):
    task_id: str
    status: str = "queued"


class TaskStatus(BaseModel):
    task_id: str
    state: str  # PENDING (also: unknown id) | STARTED | RETRY | SUCCESS | FAILURE
    result: dict[str, Any] | None = None
    error: str | None = None


@router.post(
    "/local/async",
    response_model=TaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a local-folder ingestion on the Celery worker",
)
def ingest_local_async(payload: LocalIngestRequest, db: DbSession) -> TaskAccepted:
    """Like ``/ingest/local`` but returns immediately; poll ``/ingest/tasks/{task_id}``."""
    settings = get_settings()
    _require_site(db, payload.site_id)

    folder = Path(payload.folder_path).resolve()
    if not folder.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a directory: {payload.folder_path}")
    _check_local_path_allowed(folder, settings)

    from app.tasks.ingestion_tasks import async_ingest_local_folder  # lazy: keeps the API importable without a broker

    try:
        result = async_ingest_local_folder.delay(str(folder), payload.site_id, payload.feature)
    except Exception as exc:  # noqa: BLE001 - broker down / misconfigured
        logger.exception("Could not queue ingestion task")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "The task queue is unavailable"
        ) from exc
    return TaskAccepted(task_id=result.id)


@router.get("/tasks/{task_id}", response_model=TaskStatus, summary="Status of a queued task")
def get_task_status(task_id: str) -> TaskStatus:
    from celery.result import AsyncResult

    from app.core.celery_app import celery_app

    try:
        result = AsyncResult(task_id, app=celery_app)
        state = result.state
        payload = result.result if state == "SUCCESS" else None
        error = str(result.result) if state == "FAILURE" else None
    except Exception as exc:  # noqa: BLE001 - result backend unreachable
        logger.exception("Could not read task state")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "The task result store is unavailable"
        ) from exc
    return TaskStatus(
        task_id=task_id,
        state=state,
        result=payload if isinstance(payload, dict) else None,
        error=error,
    )
