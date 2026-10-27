"""Browser imports: choose (or create) a site, upload its files, ingest them, append later.

Flow
----
1. POST   /imports               start an import into a NEW site (named by the user) or an
                                  EXISTING site (append), for one feature and time zone
2. POST   /imports/{id}/files    upload a batch of files (repeat until everything is sent)
3. POST   /imports/{id}/finish   queue ingestion on the Celery worker
4. GET    /imports/{id}          poll status and counts      (GET /imports = history)
   DELETE /imports/{id}          cancel an unfinished upload

Every uploaded file carries a path of the form ``<role>/<relative path>`` where role is
``data`` (.json), ``full`` (full-frame images) or ``crop`` (crop images). Data and images normally
come from two different folders; matching them up is done by app.services.smart_ingest using the
time / track id / plate in the file names. A data file is optional - images alone are enough.

Files are staged in MEDIA_ROOT/_uploads/<id>/ and deleted once ingested. Appending is safe to
repeat: an event that already exists for the site and feature is skipped, never overwritten.
"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi import Path as PathParam
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.db.models import ImportJob, Site
from app.db.schemas import normalize_feature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/imports", tags=["imports"])

DbSession = Annotated[Session, Depends(get_db)]
ImportId = Annotated[str, PathParam(pattern=r"^[0-9a-f]{32}$")]

MAX_FILES_PER_REQUEST = 500
MAX_PATH_DEPTH = 6
MAX_CODE_LENGTH = 20
COPY_CHUNK_BYTES = 1024 * 1024
CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ROLE_EXTENSIONS: dict[str, set[str]] = {
    "data": {".json"},
    "full": IMAGE_EXTENSIONS,
    "crop": IMAGE_EXTENSIONS,
}


# ------------------------------------------------------------------------------- helpers
def slugify_code(name: str) -> str:
    """'North Gate #2' -> 'NORTH-GATE-2' (site codes are short, upper-case identifiers)."""
    code = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-").upper()[:MAX_CODE_LENGTH].strip("-")
    return code or "SITE"


def unique_code(db: Session, base: str) -> str:
    """Return ``base`` or ``base-2``, ``base-3`` ... whichever is not taken yet."""
    candidate, number = base, 2
    while db.scalar(select(Site.id).where(Site.code == candidate)) is not None:
        suffix = f"-{number}"
        candidate = base[: MAX_CODE_LENGTH - len(suffix)] + suffix
        number += 1
    return candidate


def parse_staged_path(raw: str) -> tuple[str, PurePosixPath] | None:
    """Validate a browser-supplied ``<role>/<relative path>`` and make it safe for disk.

    Returns ``(role, relative_path)`` or ``None`` when the role is unknown, the path tries to
    escape ('..'), a name is hidden, or the file type does not belong to that role
    (JSON under ``data``; JPG/PNG under ``full`` or ``crop``).
    """
    parts = [p for p in PurePosixPath(raw.replace("\\", "/").strip().lstrip("/")).parts if p not in ("", ".")]
    if len(parts) < 2 or any(p == ".." for p in parts):
        return None
    role = parts[0].lower()
    if role not in ROLE_EXTENSIONS:
        return None
    cleaned = [re.sub(r"[^\w.\- ]", "_", p).strip() for p in parts[1:][-MAX_PATH_DEPTH:]]
    if any(not p or p.startswith(".") for p in cleaned):
        return None
    relative = PurePosixPath(*cleaned)
    if relative.suffix.lower() not in ROLE_EXTENSIONS[role]:
        return None
    return role, relative


def staging_dir(import_id: str) -> Path:
    return get_settings().uploads_dir / import_id


# ------------------------------------------------------------------------------- models
class NewSite(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    code: str | None = Field(default=None, max_length=100)  # omitted -> generated from the name


class ImportCreate(BaseModel):
    feature: str
    site_id: int | None = Field(default=None, ge=1)  # append to this site ...
    new_site: NewSite | None = None                   # ... or create this one
    # Epoch timestamps are UTC; Day/Night is decided from the clock time at the camera.
    timezone: str = Field(default="Europe/London", max_length=64)

    @field_validator("feature")
    @classmethod
    def _normalise_feature(cls, value: str) -> str:
        return normalize_feature(value)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError(f"Unknown time zone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _exactly_one_destination(self) -> ImportCreate:
        if (self.site_id is None) == (self.new_site is None):
            raise ValueError("Provide exactly one of site_id (append) or new_site (create)")
        return self


class ImportJobRead(BaseModel):
    id: str
    site_id: int
    site_name: str
    site_code: str
    feature: str
    status: str
    timezone: str = "UTC"
    created_site: bool = False
    files_received: int = 0
    bytes_received: int = 0
    events_created: int = 0
    events_from_json: int = 0      # built from a data file and its image
    events_images_only: int = 0    # built from a full image alone
    duplicates_skipped: int = 0
    invalid_records: int = 0
    issues: list[dict[str, str]] | None = None
    error: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None


class UploadResult(BaseModel):
    received: int
    skipped: int
    bytes: int
    rejected: list[str] = Field(default_factory=list)


def _read(job: ImportJob, site: Site, created_site: bool = False) -> ImportJobRead:
    return ImportJobRead(
        id=job.id, site_id=site.id, site_name=site.name, site_code=site.code,
        feature=job.feature, status=job.status, timezone=job.timezone, created_site=created_site,
        files_received=job.files_received, bytes_received=job.bytes_received,
        events_created=job.events_created, events_from_json=job.events_from_json,
        events_images_only=job.events_images_only, duplicates_skipped=job.duplicates_skipped,
        invalid_records=job.invalid_records, issues=job.issues, error=job.error,
        created_at=job.created_at, finished_at=job.finished_at,
    )


def _job_or_404(db: Session, import_id: str) -> tuple[ImportJob, Site]:
    job = db.get(ImportJob, import_id)
    site = db.get(Site, job.site_id) if job else None
    if job is None or site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import not found")
    return job, site


# --------------------------------------------------------------------------------- routes
_CREATE = {"response_model": ImportJobRead, "status_code": status.HTTP_201_CREATED,
           "summary": "Start an import into a new or existing site"}


@router.post("", **_CREATE)
@router.post("/", include_in_schema=False, **_CREATE)
def create_import(payload: ImportCreate, db: DbSession) -> ImportJobRead:
    created_site = False
    if payload.site_id is not None:
        site = db.get(Site, payload.site_id)
        if site is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Site {payload.site_id} not found")
    else:
        assert payload.new_site is not None
        name = payload.new_site.name.strip()
        if not name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Give the new site a name")
        if payload.new_site.code and payload.new_site.code.strip():
            code = payload.new_site.code.strip()
            if not CODE_RE.match(code):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Site code may only contain letters, digits, '.', '_' and '-'",
                )
            if db.scalar(select(Site.id).where(Site.code == code)) is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, f"A site with code {code!r} already exists")
        else:
            code = unique_code(db, slugify_code(name))
        site = Site(name=name, code=code)
        db.add(site)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, f"A site with code {code!r} already exists") from exc
        created_site = True

    job = ImportJob(
        id=uuid.uuid4().hex, site_id=site.id, feature=payload.feature,
        status="uploading", timezone=payload.timezone,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return _read(job, site, created_site)


@router.post("/{import_id}/files", response_model=UploadResult, summary="Upload a batch of files")
def upload_files(
    import_id: ImportId,
    db: DbSession,
    files: Annotated[list[UploadFile], File(description="Data (.json) and image files")],
    paths: Annotated[list[str] | None, Form(description="'<role>/<relative path>' per file, same order")] = None,
) -> UploadResult:
    settings = get_settings()
    job, _site = _job_or_404(db, import_id)
    if job.status != "uploading":
        raise HTTPException(status.HTTP_409_CONFLICT, "This import is no longer accepting files")
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"At most {MAX_FILES_PER_REQUEST} files per request")
    if paths is not None and len(paths) != len(files):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "paths must match files one-to-one")

    max_file = settings.upload_max_file_mb * 1024 * 1024
    max_import = settings.upload_max_import_gb * 1024 ** 3
    root = staging_dir(import_id)
    root.mkdir(parents=True, exist_ok=True)

    received = skipped = saved_bytes = 0
    rejected: list[str] = []

    for index, upload in enumerate(files):
        parsed = parse_staged_path((paths[index] if paths else None) or "")
        if parsed is None:
            skipped += 1
            continue
        role, relative = parsed

        target = root / role / Path(*relative.parts)
        if not target.resolve().is_relative_to(root.resolve()):  # belt and braces
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")

        size, too_large = 0, False
        try:
            with tmp.open("wb") as out:
                while chunk := upload.file.read(COPY_CHUNK_BYTES):
                    size += len(chunk)
                    if size > max_file:
                        too_large = True
                        break
                    out.write(chunk)
            if too_large:
                tmp.unlink(missing_ok=True)
                skipped += 1
                rejected.append(f"{relative.name} (over {settings.upload_max_file_mb} MB)")
                continue
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)

        received += 1
        saved_bytes += size
        if job.bytes_received + saved_bytes > max_import:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"This import exceeds the {settings.upload_max_import_gb} GB limit",
            )

    job.bytes_received += saved_bytes
    job.files_received += received
    db.commit()
    return UploadResult(received=received, skipped=skipped, bytes=saved_bytes, rejected=rejected)


@router.post("/{import_id}/finish", response_model=ImportJobRead, summary="Queue ingestion")
def finish_import(import_id: ImportId, db: DbSession) -> ImportJobRead:
    job, _site = _job_or_404(db, import_id)
    if job.status != "uploading":
        raise HTTPException(status.HTTP_409_CONFLICT, "This import was already finished")

    root = staging_dir(import_id)
    staged = [p for p in root.rglob("*") if p.is_file() and not p.name.endswith(".part")] if root.is_dir() else []
    if not any(p.relative_to(root).parts[0] == "full" for p in staged):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No full images were uploaded. Every event needs a full image to be reviewed.",
        )

    job.files_received = len(staged)  # authoritative: counted from disk, not from retries
    job.bytes_received = sum(p.stat().st_size for p in staged)
    job.status = "queued"
    db.commit()

    from app.tasks.ingestion_tasks import async_ingest_upload  # lazy: keeps the API importable without a broker

    try:
        job.task_id = async_ingest_upload.delay(import_id).id
        db.commit()
    except Exception:  # noqa: BLE001 - broker unreachable (e.g. running without Redis)
        logger.warning("Task queue unavailable; ingesting import %s inline", import_id, exc_info=True)
        async_ingest_upload.apply(args=(import_id,), throw=False)

    db.expire_all()  # the task updated the row from its own session
    job, site = _job_or_404(db, import_id)
    return _read(job, site)


@router.get("/{import_id}", response_model=ImportJobRead, summary="Import status")
def get_import(import_id: ImportId, db: DbSession) -> ImportJobRead:
    job, site = _job_or_404(db, import_id)
    return _read(job, site)


@router.get("", response_model=list[ImportJobRead], summary="Recent imports")
def list_imports(
    db: DbSession,
    site_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ImportJobRead]:
    stmt = (
        select(ImportJob, Site)
        .join(Site, Site.id == ImportJob.site_id)
        .order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
        .limit(limit)
    )
    if site_id is not None:
        stmt = stmt.where(ImportJob.site_id == site_id)
    return [_read(job, site) for job, site in db.execute(stmt)]


@router.delete("/{import_id}", response_model=ImportJobRead, summary="Cancel an unfinished upload")
def cancel_import(import_id: ImportId, db: DbSession) -> ImportJobRead:
    job, site = _job_or_404(db, import_id)
    if job.status != "uploading":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only an upload in progress can be cancelled")
    shutil.rmtree(staging_dir(import_id), ignore_errors=True)
    job.status = "failed"
    job.error = "Cancelled by the user"
    job.finished_at = datetime.utcnow()
    db.commit()
    return _read(job, site)
