"""Browse every event that is still on a site, and delete a site.

    GET    /browse/events               search / filter / sort / page through events (Sites page)
    PUT    /events/{event_id}/review    save or change the verdict of ANY event (create-or-update)
    DELETE /events/{event_id}/review    take a verdict back: the event is "not reviewed" again
    DELETE /sites/{site_id}             delete a site with everything in it

This router is mounted by app.api.v1.analytics, so the full paths start with /analytics.

Events stay browsable (with their images) until the user removes them: by deleting the site, or by
confirming "remove" after downloading a classified zip. Events removed that way live on only as
rows in the analysis and the .xlsx export; they have no images, so they are not listed here.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.classified_exports import _remove_empty_parents
from app.core.config import get_settings
from app.core.database import get_db
from app.db.models import ArchivedEvent, Event, EventReview, ImportJob, Site
from app.db.schemas import normalize_feature
from app.services.classified_export import resolve_media_file

logger = logging.getLogger(__name__)
router = APIRouter(tags=["sites"])

DbSession = Annotated[Session, Depends(get_db)]
SiteId = Annotated[int, PathParam(ge=1)]

REVIEW_STATUSES = {"reviewed", "unreviewed", "TP", "FP", "Unsure"}
SORTS = {
    "time": Event.offence_timestamp,
    "type": Event.feature,
    "plate": Event.vrm,
    "day_night": Event.day_night,
    "make": Event.vehicle_make,
    "model": Event.vehicle_model,
    "color": Event.vehicle_color,
    "review": EventReview.human_label,
    "site": Site.name,
}


class ReviewBrief(BaseModel):
    human_label: str
    image_clarity_clear: bool | None = None
    vrm_correct: bool | None = None
    make_correct: bool | None = None
    model_correct: bool | None = None
    color_correct: bool | None = None
    reviewed_at: datetime | None = None


class EventRow(BaseModel):
    id: int
    site_id: int
    site_name: str
    case_id: str
    feature: str
    vrm: str | None = None
    offence_timestamp: datetime
    day_night: str
    vehicle_make: str | None = None
    vehicle_model: str | None = None
    vehicle_color: str | None = None
    make_confidence: float | None = None
    model_confidence: float | None = None
    color_confidence: float | None = None
    full_image_url: str
    crop_image_url: str
    review: ReviewBrief | None = None


class EventPage(BaseModel):
    items: list[EventRow]
    total: int                 # events matching the filters
    site_total: int            # events in the chosen site (or all sites) before filtering
    archived_total: int        # records kept only for analytics (removed after a download, or imported)
    features: list[str]        # violation types present, for the filter
    limit: int
    offset: int


class SiteDeleted(BaseModel):
    site_id: int
    events: int
    reviews: int
    archived: int
    files_removed: int


def _like(column: Any, term: str) -> Any:
    return func.lower(func.coalesce(column, "")).contains(term.lower(), autoescape=True)


@router.get("/browse/events", response_model=EventPage, summary="Search the events on a site")
def browse_events(
    db: DbSession,
    site_id: Annotated[int | None, Query(ge=1)] = None,
    feature: str | None = None,
    plate: str | None = None,
    search: str | None = None,
    day_night: Literal["Day", "Night"] | None = None,
    review: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    sort: Annotated[str, Query()] = "time",
    order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=500)] = 10,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventPage:
    if sort not in SORTS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Cannot sort by {sort!r}")
    if review and review not in REVIEW_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown review filter {review!r}")
    wanted: str | None = None
    if feature and feature.strip():
        try:
            wanted = normalize_feature(feature)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown violation type {feature!r}") from exc

    scope = [Event.site_id == site_id] if site_id is not None else []
    conditions = list(scope)
    if wanted:
        conditions.append(Event.feature == wanted)
    if plate and plate.strip():
        conditions.append(_like(Event.vrm, plate.strip().replace(" ", "")))
    if day_night:
        conditions.append(Event.day_night == day_night)
    if date_from:
        conditions.append(Event.offence_timestamp >= date_from)
    if date_to:
        conditions.append(Event.offence_timestamp <= date_to)
    if review == "reviewed":
        conditions.append(EventReview.id.is_not(None))
    elif review == "unreviewed":
        conditions.append(EventReview.id.is_(None))
    elif review:
        conditions.append(EventReview.human_label == review)
    if search and search.strip():
        term = search.strip()
        conditions.append(or_(*(_like(column, term) for column in (
            Event.vrm, Event.case_id, Event.feature, Event.vehicle_make, Event.vehicle_model,
            Event.vehicle_color, Event.day_night, Site.name, EventReview.human_label))))

    base = (
        select(Event, EventReview, Site.name)
        .join(Site, Site.id == Event.site_id)
        .outerjoin(EventReview, EventReview.event_id == Event.id)
        .where(*conditions)
    )
    total = db.scalar(
        select(func.count(Event.id))
        .select_from(Event)
        .join(Site, Site.id == Event.site_id)
        .outerjoin(EventReview, EventReview.event_id == Event.id)
        .where(*conditions)
    ) or 0

    column = SORTS[sort]
    ordering = column.asc() if order == "asc" else column.desc()
    rows = db.execute(base.order_by(ordering, Event.id.desc()).limit(limit).offset(offset))

    items = [
        EventRow(
            id=event.id, site_id=event.site_id, site_name=site_name, case_id=event.case_id,
            feature=event.feature, vrm=event.vrm, offence_timestamp=event.offence_timestamp,
            day_night=event.day_night, vehicle_make=event.vehicle_make,
            vehicle_model=event.vehicle_model, vehicle_color=event.vehicle_color,
            make_confidence=event.make_confidence, model_confidence=event.model_confidence,
            color_confidence=event.color_confidence, full_image_url=event.full_image_url,
            crop_image_url=event.crop_image_url,
            review=None if review_row is None else ReviewBrief(
                human_label=review_row.human_label, image_clarity_clear=review_row.image_clarity_clear,
                vrm_correct=review_row.vrm_correct, make_correct=review_row.make_correct,
                model_correct=review_row.model_correct, color_correct=review_row.color_correct,
                reviewed_at=review_row.reviewed_at,
            ),
        )
        for event, review_row, site_name in rows
    ]

    archived_stmt = select(func.count(ArchivedEvent.id)).where(
        *([ArchivedEvent.site_id == site_id] if site_id is not None else []))
    return EventPage(
        items=items,
        total=int(total),
        site_total=int(db.scalar(select(func.count(Event.id)).where(*scope)) or 0),
        archived_total=int(db.scalar(archived_stmt) or 0),
        features=sorted(db.scalars(select(Event.feature).where(*scope).distinct())),
        limit=limit,
        offset=offset,
    )


@router.delete("/sites/{site_id}", response_model=SiteDeleted, summary="Delete a site and all its data")
def delete_site(site_id: SiteId, db: DbSession) -> SiteDeleted:
    settings = get_settings()
    if db.get(Site, site_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Site {site_id} not found")

    running = db.scalar(select(func.count(ImportJob.id)).where(
        ImportJob.site_id == site_id, ImportJob.status.in_(("queued", "processing"))))
    if running:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "An import into this site is still running. Try again when it has finished.")

    urls: set[str] = set()
    for full_url, crop_url in db.execute(
        select(Event.full_image_url, Event.crop_image_url).where(Event.site_id == site_id)
    ):
        urls.update(u for u in (full_url, crop_url) if u)
    job_ids = list(db.scalars(select(ImportJob.id).where(ImportJob.site_id == site_id)))

    event_ids = select(Event.id).where(Event.site_id == site_id)
    reviews = db.execute(delete(EventReview).where(EventReview.event_id.in_(event_ids))
                         .execution_options(synchronize_session=False)).rowcount or 0
    events = db.execute(delete(Event).where(Event.site_id == site_id)
                        .execution_options(synchronize_session=False)).rowcount or 0
    archived = db.execute(delete(ArchivedEvent).where(ArchivedEvent.site_id == site_id)
                          .execution_options(synchronize_session=False)).rowcount or 0
    db.execute(delete(ImportJob).where(ImportJob.site_id == site_id).execution_options(synchronize_session=False))
    db.execute(delete(Site).where(Site.id == site_id).execution_options(synchronize_session=False))
    db.commit()

    # Files go after the database change is committed; a leftover file is harmless, a missing
    # row that still pointed at a deleted file would not be.
    media_root = settings.media_root.resolve()
    removed = 0
    for url in urls:
        file = resolve_media_file(url, settings.media_root, settings.media_url_prefix)
        if file is None:
            continue
        try:
            file.unlink()
            removed += 1
            _remove_empty_parents(file, media_root)
        except OSError:
            logger.warning("Could not remove image %s", file)
    for job_id in job_ids:
        shutil.rmtree(settings.uploads_dir / job_id, ignore_errors=True)

    logger.info("Deleted site %s: %d events, %d reviews, %d archived, %d files", site_id, events, reviews, archived, removed)
    return SiteDeleted(site_id=site_id, events=int(events), reviews=int(reviews),
                       archived=int(archived), files_removed=removed)


# ----------------------------------------------------------------------------------- reviews
class ReviewIn(BaseModel):
    human_label: Literal["TP", "FP", "Unsure"]
    reviewer_email: str | None = Field(default=None, max_length=255)
    # tick = True, cross = False, skipped = None
    vrm_correct: bool | None = None
    make_correct: bool | None = None
    model_correct: bool | None = None
    color_correct: bool | None = None
    image_clarity_clear: bool | None = None   # False = unclear


EventId = Annotated[int, PathParam(ge=1)]


@router.put("/events/{event_id}/review", response_model=ReviewBrief, summary="Save an event's verdict")
def save_review(event_id: EventId, payload: ReviewIn, db: DbSession) -> ReviewBrief:
    """Create the review, or replace it when the event was reviewed before. Works for any event, so a
    reviewed one can be revisited and changed, and a missed one can be reviewed later."""
    if db.get(Event, event_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Event {event_id} not found (it may have been removed)")

    for attempt in (1, 2):
        review = db.scalar(select(EventReview).where(EventReview.event_id == event_id))
        if review is None:
            review = EventReview(event_id=event_id, reviewer_email="unknown", human_label=payload.human_label)
            db.add(review)
        review.human_label = payload.human_label
        review.reviewer_email = (payload.reviewer_email or "").strip() or review.reviewer_email or "unknown"
        review.vrm_correct = payload.vrm_correct
        review.make_correct = payload.make_correct
        review.model_correct = payload.model_correct
        review.color_correct = payload.color_correct
        review.image_clarity_clear = payload.image_clarity_clear
        review.reviewed_at = datetime.utcnow()
        try:
            db.commit()
            break
        except IntegrityError:  # two saves raced to create the same review: retry as an update
            db.rollback()
            if attempt == 2:
                raise HTTPException(status.HTTP_409_CONFLICT, "Another save for this event was in progress. Try again.")
    db.refresh(review)
    return ReviewBrief(
        human_label=review.human_label, image_clarity_clear=review.image_clarity_clear,
        vrm_correct=review.vrm_correct, make_correct=review.make_correct,
        model_correct=review.model_correct, color_correct=review.color_correct,
        reviewed_at=review.reviewed_at,
    )


@router.delete("/events/{event_id}/review", status_code=status.HTTP_204_NO_CONTENT, summary="Clear an event's verdict")
def clear_review(event_id: EventId, db: DbSession) -> None:
    if db.get(Event, event_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Event {event_id} not found")
    db.execute(delete(EventReview).where(EventReview.event_id == event_id).execution_options(synchronize_session=False))
    db.commit()
