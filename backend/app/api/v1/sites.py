"""Site endpoints: list and create camera sites."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import Event, EventReview, Site
from app.db.schemas import SiteCreate, SiteRead

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sites", tags=["sites"])

DbSession = Annotated[Session, Depends(get_db)]


@router.get("", response_model=list[SiteRead], summary="List all sites")
@router.get("/", response_model=list[SiteRead], include_in_schema=False)
def list_sites(db: DbSession) -> list[Site]:
    return list(db.scalars(select(Site).order_by(Site.name, Site.id)).all())


@router.post(
    "",
    response_model=SiteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a site",
)
@router.post(
    "/",
    response_model=SiteRead,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def create_site(payload: SiteCreate, db: DbSession) -> Site:
    name, code = payload.name.strip(), payload.code.strip()
    if not name or not code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Site name and code must not be blank")

    if db.scalar(select(Site.id).where(Site.code == code)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"A site with code {code!r} already exists")

    site = Site(name=name, code=code)
    db.add(site)
    try:
        db.commit()
    except IntegrityError as exc:  # lost a race with a concurrent create
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A site with code {code!r} already exists"
        ) from exc
    db.refresh(site)
    return site


class SiteStats(BaseModel):
    """A site with how much data it holds and how much of it has been reviewed."""

    id: int
    name: str
    code: str
    created_at: datetime
    event_count: int = 0
    reviewed_count: int = 0
    pending_count: int = 0
    last_ingested_at: datetime | None = None


@router.get("/stats", response_model=list[SiteStats], summary="Sites with event and review counts")
def list_site_stats(db: DbSession) -> list[SiteStats]:
    """Used by the Import page so users can see what they are appending to."""
    events = (
        select(
            Event.site_id.label("site_id"),
            func.count(Event.id).label("events"),
            func.max(Event.ingested_at).label("last_ingested_at"),
        )
        .group_by(Event.site_id)
        .subquery()
    )
    reviewed = (
        select(Event.site_id.label("site_id"), func.count(EventReview.id).label("reviewed"))
        .join(EventReview, EventReview.event_id == Event.id)
        .group_by(Event.site_id)
        .subquery()
    )
    rows = db.execute(
        select(Site, events.c.events, events.c.last_ingested_at, reviewed.c.reviewed)
        .outerjoin(events, events.c.site_id == Site.id)
        .outerjoin(reviewed, reviewed.c.site_id == Site.id)
        .order_by(Site.name, Site.id)
    ).all()

    stats: list[SiteStats] = []
    for site, event_count, last_ingested_at, reviewed_count in rows:
        total, done = int(event_count or 0), int(reviewed_count or 0)
        stats.append(SiteStats(
            id=site.id, name=site.name, code=site.code, created_at=site.created_at,
            event_count=total, reviewed_count=done, pending_count=max(0, total - done),
            last_ingested_at=last_ingested_at,
        ))
    return stats
