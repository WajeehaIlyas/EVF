"""Analytics endpoint: precision, recall, per-check accuracy and Day/Night breakdowns."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import Site
from app.db.schemas import normalize_feature
from app.services.analytics_core import AnalyticsService, MetricsSummary

router = APIRouter(prefix="/metrics", tags=["metrics"])

DbSession = Annotated[Session, Depends(get_db)]


@router.get("/summary", response_model=MetricsSummary, summary="Review accuracy summary")
def get_metrics_summary(
    db: DbSession,
    site_id: Annotated[int, Query(ge=1, description="Site to analyse")],
    feature: Annotated[
        str | None, Query(description="e.g. seatbelt; omit for all features")
    ] = None,
    false_negatives: Annotated[
        int | None,
        Query(ge=0, description="Known missed detections (FN); required to compute recall"),
    ] = None,
) -> MetricsSummary:
    """Computed live from the reviews table.

    Percentages are ``null`` (not 0) when there is nothing to divide by. Recall is ``null``
    unless ``false_negatives`` is supplied, because reviews never record missed detections.
    """
    if db.get(Site, site_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Site {site_id} not found")

    feature_key: str | None = None
    if feature:
        try:
            feature_key = normalize_feature(feature)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return AnalyticsService(db).summary(site_id, feature_key, false_negatives)
