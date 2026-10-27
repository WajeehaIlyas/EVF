"""Download the reviewed events as a classified zip, then (optionally) remove them from the site.

    GET    /classified-exports/preview      how many reviewed events would go in which folder
    POST   /classified-exports              build the zip  -> { token, filename, counts, ... }
    GET    /classified-exports/{token}/download   the zip file (the browser saves it)
    POST   /classified-exports/{token}/finalize   the user has the zip: archive + remove those events
    DELETE /classified-exports/{token}      throw the zip away, change nothing

Why two steps (build, then finalize)? The server cannot tell whether a browser finished saving a
file, so removing events is a separate, explicit confirmation. Finalize removes exactly the events
that are inside the zip, nothing reviewed after it was built, and an event whose verdict was
edited since is left alone. What is removed: the Event + review rows and their image files. What
stays: a slim ``archived_events`` row per event, so every figure on the Analytics page is unchanged.

Events that are still waiting for review are never exported or removed.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.db.models import ArchivedEvent, Event, EventReview, Site
from app.db.schemas import normalize_feature
from app.services.xlsx_export import track_from_case_id
from app.services.classified_export import (
    FOLDERS,
    ExportItem,
    build_zip,
    folder_for,
    resolve_media_file,
    safe_name,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/classified-exports", tags=["exports"])

DbSession = Annotated[Session, Depends(get_db)]
Token = Annotated[str, PathParam(pattern=r"^[0-9a-f]{32}$")]


# ------------------------------------------------------------------------------- helpers
def exports_dir() -> Path:
    return get_settings().uploads_dir / "_exports"


def _manifest_path(token: str) -> Path:
    return exports_dir() / f"{token}.json"


def _zip_path(token: str) -> Path:
    return exports_dir() / f"{token}.zip"


def _fingerprint(review: EventReview) -> list[Any]:
    """Everything that decides which folder an event is in, plus the review time."""
    return [
        review.human_label, review.vrm_correct, review.make_correct, review.model_correct,
        review.color_correct, review.image_clarity_clear,
        review.reviewed_at.isoformat() if review.reviewed_at else None,
    ]


def _normalise_feature(feature: str | None) -> str | None:
    if not feature or not feature.strip():
        return None
    try:
        return normalize_feature(feature)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown feature {feature!r}") from exc


def _reviewed(db: Session, site_id: int, feature: str | None) -> list[tuple[Event, EventReview]]:
    if db.get(Site, site_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Site {site_id} not found")
    stmt = (
        select(Event, EventReview)
        .join(EventReview, EventReview.event_id == Event.id)
        .where(Event.site_id == site_id)
        .order_by(Event.feature, Event.offence_timestamp, Event.id)
    )
    if feature:
        stmt = stmt.where(Event.feature == feature)
    return [(event, review) for event, review in db.execute(stmt)]


class Preview(BaseModel):
    reviewed: int
    unreviewed: int
    counts: dict[str, int]


class ExportCreate(BaseModel):
    site_id: int = Field(ge=1)
    feature: str | None = None          # omitted: every feature of the site
    name: str | None = Field(default=None, max_length=120)
    include_crops: bool = False


class ExportRead(BaseModel):
    token: str
    name: str
    filename: str
    events: int
    counts: dict[str, int]
    missing_images: int
    zip_bytes: int


class FinalizeResult(BaseModel):
    archived: int
    skipped_changed: int
    files_removed: int


# --------------------------------------------------------------------------------- routes
@router.get("/preview", response_model=Preview, summary="What a download would contain")
def preview(
    db: DbSession,
    site_id: Annotated[int, Query(ge=1)],
    feature: str | None = None,
) -> Preview:
    wanted = _normalise_feature(feature)
    counts = {folder: 0 for folder in FOLDERS}
    pairs = _reviewed(db, site_id, wanted)
    for _event, review in pairs:
        counts[folder_for(review.human_label, review.image_clarity_clear)] += 1
    stmt = (
        select(func.count(Event.id))
        .outerjoin(EventReview, EventReview.event_id == Event.id)
        .where(Event.site_id == site_id, EventReview.id.is_(None))
    )
    if wanted:
        stmt = stmt.where(Event.feature == wanted)
    return Preview(reviewed=len(pairs), unreviewed=int(db.scalar(stmt) or 0), counts=counts)


@router.post("", response_model=ExportRead, status_code=status.HTTP_201_CREATED,
             summary="Build the classified zip")
def create_export(payload: ExportCreate, db: DbSession) -> ExportRead:
    settings = get_settings()
    wanted = _normalise_feature(payload.feature)
    pairs = _reviewed(db, payload.site_id, wanted)
    if not pairs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No events have been reviewed yet, so there is nothing to download")

    name = safe_name(payload.name)
    items = [
        ExportItem(
            event_id=event.id, case_id=event.case_id, label=review.human_label,
            clear=review.image_clarity_clear, full_url=event.full_image_url,
            crop_url=event.crop_image_url, vrm=event.vrm,
            timestamp=event.offence_timestamp.isoformat(sep=" ") if event.offence_timestamp else "",
            answers={"vrm": review.vrm_correct, "make": review.make_correct,
                     "model": review.model_correct, "color": review.color_correct},
        )
        for event, review in pairs
    ]
    token = uuid.uuid4().hex
    result = build_zip(_zip_path(token), name, items, settings.media_root,
                       settings.media_url_prefix, payload.include_crops)
    if not result.included:
        _zip_path(token).unlink(missing_ok=True)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "None of the reviewed images could be found on disk")

    included = set(result.included)
    manifest = {
        "site_id": payload.site_id,
        "feature": wanted,
        "name": name,
        "created": datetime.now(timezone.utc).isoformat(),
        "events": [
            {"id": event.id, "fp": _fingerprint(review)}
            for event, review in pairs if event.id in included
        ],
    }
    _manifest_path(token).write_text(json.dumps(manifest), encoding="utf-8")
    return ExportRead(
        token=token, name=name, filename=f"{name}.zip", events=len(result.included),
        counts=result.counts, missing_images=len(result.missing),
        zip_bytes=_zip_path(token).stat().st_size,
    )


def _load_manifest(token: str) -> dict[str, Any]:
    try:
        return json.loads(_manifest_path(token).read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (OSError, ValueError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This download has expired. Build the zip again.") from exc


@router.get("/{token}/download", summary="The zip file")
def download(token: Token) -> FileResponse:
    manifest = _load_manifest(token)
    path = _zip_path(token)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This download has expired. Build the zip again.")
    return FileResponse(path, media_type="application/zip", filename=f"{manifest['name']}.zip")


def _remove_empty_parents(path: Path, stop: Path) -> None:
    parent = path.parent
    while parent != stop and stop in parent.parents:
        try:
            parent.rmdir()  # only succeeds when empty
        except OSError:
            return
        parent = parent.parent


@router.post("/{token}/finalize", response_model=FinalizeResult,
             summary="The zip is saved: archive its events and remove them from the site")
def finalize(token: Token, db: DbSession) -> FinalizeResult:
    settings = get_settings()
    manifest = _load_manifest(token)
    site_id: int = manifest["site_id"]

    archived = skipped = 0
    urls: set[str] = set()
    touched_features: set[str] = set()
    ids = [entry["id"] for entry in manifest["events"]]

    for start in range(0, len(ids), 500):
        chunk = {entry["id"]: entry["fp"] for entry in manifest["events"][start:start + 500]}
        events = {
            event.id: event
            for event in db.scalars(select(Event).where(Event.site_id == site_id, Event.id.in_(list(chunk))))
        }
        reviews = {
            review.event_id: review
            for review in db.scalars(select(EventReview).where(EventReview.event_id.in_(list(chunk))))
        }
        known = {
            (record.feature, record.case_id): record
            for record in db.scalars(
                select(ArchivedEvent).where(
                    ArchivedEvent.site_id == site_id,
                    ArchivedEvent.case_id.in_([e.case_id for e in events.values()]),
                )
            )
        }
        for event_id, fingerprint in chunk.items():
            event, review = events.get(event_id), reviews.get(event_id)
            if event is None or review is None or _fingerprint(review) != fingerprint:
                skipped += 1  # already gone, or the verdict was edited after the zip was built
                continue
            record = known.get((event.feature, event.case_id))
            if record is None:
                record = ArchivedEvent(site_id=site_id, feature=event.feature, case_id=event.case_id)
                db.add(record)
            record.source = "review"
            record.track_id = track_from_case_id(event.case_id)
            record.vehicle_make = event.vehicle_make
            record.make_confidence = event.make_confidence
            record.vehicle_model = event.vehicle_model
            record.model_confidence = event.model_confidence
            record.vehicle_color = event.vehicle_color
            record.color_confidence = event.color_confidence
            record.vrm = event.vrm
            record.offence_timestamp = event.offence_timestamp
            record.day_night = event.day_night
            record.human_label = review.human_label
            record.vrm_correct = review.vrm_correct
            record.make_correct = review.make_correct
            record.model_correct = review.model_correct
            record.color_correct = review.color_correct
            record.image_clarity_clear = review.image_clarity_clear

            urls.update(u for u in (event.full_image_url, event.crop_image_url) if u)
            touched_features.add(event.feature)
            db.delete(review)
            db.delete(event)
            archived += 1
        db.flush()  # ArchivedEvent rows and deletions are sent per chunk, one commit at the end
    db.commit()

    # Image files go last, and only after the database change is safely committed. A file that
    # another remaining event still points at is kept.
    still_used: set[str] = set()
    for feature in touched_features:
        for full_url, crop_url in db.execute(
            select(Event.full_image_url, Event.crop_image_url).where(
                Event.site_id == site_id, Event.feature == feature
            )
        ):
            still_used.update((full_url, crop_url))
    media_root = settings.media_root.resolve()
    removed = 0
    for url in urls - still_used:
        file = resolve_media_file(url, settings.media_root, settings.media_url_prefix)
        if file is None:
            continue
        try:
            file.unlink()
            removed += 1
            _remove_empty_parents(file, media_root)
        except OSError:
            logger.warning("Could not remove image %s", file)

    _zip_path(token).unlink(missing_ok=True)
    _manifest_path(token).unlink(missing_ok=True)
    return FinalizeResult(archived=archived, skipped_changed=skipped, files_removed=removed)


@router.delete("/{token}", status_code=status.HTTP_204_NO_CONTENT, summary="Discard a built zip")
def discard(token: Token) -> None:
    _zip_path(token).unlink(missing_ok=True)
    _manifest_path(token).unlink(missing_ok=True)
