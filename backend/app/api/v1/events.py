"""Event endpoints: the review queue and single-event detail."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.database import get_db
from app.db.models import Event, EventReview, Site
from app.db.schemas import EventDetail, EventQueueResponse, normalize_feature

router = APIRouter(prefix="/events", tags=["events"])

DbSession = Annotated[Session, Depends(get_db)]


# ------------------------------------------------------------------------------ helpers
def _base_url(request: Request) -> str:
    """Public origin used to absolutise media paths (setting wins over the request)."""
    return (get_settings().public_base_url or str(request.base_url)).rstrip("/")


def _absolute_media_url(url: str, base: str) -> str:
    """Turn a stored ``/media/...`` path into ``<base>/media/...``; leave full URLs alone.

    The React app runs on another origin (localhost:3000), so a bare ``/media/...`` would
    resolve against the *frontend* host. Absolute URLs always hit the API's static mount.
    """
    if url.lower().startswith(("http://", "https://")):
        return url
    return f"{base}/{url.lstrip('/')}"


def _to_detail(event: Event, base: str) -> EventDetail:
    """Serialise an ORM event without mutating it, with absolute image URLs."""
    detail = EventDetail.model_validate(event)
    return detail.model_copy(
        update={
            "full_image_url": _absolute_media_url(detail.full_image_url, base),
            "crop_image_url": _absolute_media_url(detail.crop_image_url, base),
        }
    )


# ---------------------------------------------------------------------------------- routes
# NOTE: /queue must be declared before /{event_id} so "queue" is not parsed as an id.
@router.get("/queue", response_model=EventQueueResponse, summary="Event review queue")
def get_event_queue(
    request: Request,
    db: DbSession,
    site_id: Annotated[int, Query(ge=1, description="Site to load events for")],
    feature: Annotated[str, Query(min_length=1, description="e.g. seatbelt, mobilephone")],
    status_filter: Annotated[
        Literal["unreviewed", "reviewed"],
        Query(alias="status", description="Queue to load (default: unreviewed)"),
    ] = "unreviewed",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventQueueResponse:
    """Return events for a site/feature, oldest offence first.

    * ``unreviewed`` (default): events with no review, ordered by offence time. As events get
      reviewed they drop out of this list, so clients should keep ``offset=0`` and re-fetch
      rather than paging forward.
    * ``reviewed``: most recently reviewed first, each item carrying its ``review``.
    """
    try:
        feature_key = normalize_feature(feature)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if db.get(Site, site_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Site {site_id} not found")

    reviewed = status_filter == "reviewed"
    conditions = [
        Event.site_id == site_id,
        Event.feature == feature_key,
        Event.review.has() if reviewed else ~Event.review.has(),
    ]

    total = db.scalar(select(func.count(Event.id)).where(*conditions)) or 0

    stmt = select(Event).where(*conditions).options(selectinload(Event.review))
    if reviewed:
        stmt = stmt.join(EventReview, EventReview.event_id == Event.id).order_by(
            EventReview.reviewed_at.desc(), Event.id.desc()
        )
    else:
        stmt = stmt.order_by(Event.offence_timestamp, Event.id)
    events = db.scalars(stmt.limit(limit).offset(offset)).all()

    base = _base_url(request)
    return EventQueueResponse(
        items=[_to_detail(e, base) for e in events],
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(events) < total,
    )


@router.get("/{event_id}", response_model=EventDetail, summary="Single event detail")
def get_event(
    request: Request,
    db: DbSession,
    event_id: Annotated[int, Path(ge=1)],
) -> EventDetail:
    event = db.scalar(
        select(Event).where(Event.id == event_id).options(selectinload(Event.review))
    )
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Event {event_id} not found")
    return _to_detail(event, _base_url(request))
