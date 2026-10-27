"""Review endpoint: create-or-update a human decision for an event."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import Event, EventReview
from app.db.schemas import ReviewCreate, ReviewRead, ReviewSaveResult

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reviews", tags=["reviews"])

DbSession = Annotated[Session, Depends(get_db)]

TOGGLE_FIELDS = (
    "vrm_correct",
    "make_correct",
    "model_correct",
    "color_correct",
    "image_clarity_clear",
)


def _get_review(db: Session, event_id: int) -> EventReview | None:
    # FOR UPDATE serialises concurrent saves of the same event on PostgreSQL
    # (SQLAlchemy omits it on backends that lack it, e.g. SQLite in tests).
    return db.scalar(
        select(EventReview).where(EventReview.event_id == event_id).with_for_update()
    )


def _apply(review: EventReview, payload: ReviewCreate) -> None:
    """Copy the payload onto the ORM row.

    ``human_label`` and ``reviewer_email`` always apply. A tick/cross toggle is only touched
    if the client *sent* it: omitted = leave unchanged, explicit ``null`` = mark as skipped.
    """
    review.human_label = payload.human_label
    review.reviewer_email = str(payload.reviewer_email).strip().lower()
    for name in TOGGLE_FIELDS:
        if name in payload.model_fields_set:
            setattr(review, name, getattr(payload, name))


_SAVE_ROUTE = {
    "response_model": ReviewSaveResult,
    "summary": "Save (create or update) a review",
}


@router.post("", **_SAVE_ROUTE)
@router.post("/", include_in_schema=False, **_SAVE_ROUTE)
def save_review(payload: ReviewCreate, response: Response, db: DbSession) -> ReviewSaveResult:
    """Upsert the single review for ``event_id``. 201 when created, 200 when updated."""
    if db.get(Event, payload.event_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Event {payload.event_id} not found")

    review = _get_review(db, payload.event_id)
    created = review is None
    if review is None:
        review = EventReview(event_id=payload.event_id)
        _apply(review, payload)
        db.add(review)
    else:
        _apply(review, payload)
        review.reviewed_at = func.current_timestamp()  # type: ignore[assignment]

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Either a concurrent request inserted the review first, or the event vanished.
        if db.get(Event, payload.event_id) is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"Event {payload.event_id} not found"
            ) from exc
        review = _get_review(db, payload.event_id)
        if review is None:
            logger.exception("Review insert failed for event %s", payload.event_id)
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not save review"
            ) from exc
        created = False
        _apply(review, payload)
        review.reviewed_at = func.current_timestamp()  # type: ignore[assignment]
        db.commit()

    db.refresh(review)  # load server-generated reviewed_at
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return ReviewSaveResult(
        status="created" if created else "updated",
        event_id=review.event_id,
        human_label=review.human_label,  # type: ignore[arg-type]
        reviewed_at=review.reviewed_at,
        review=ReviewRead.model_validate(review),
    )
