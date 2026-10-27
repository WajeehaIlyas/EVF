"""Load the rows behind the Analytics page from the database.

An analysis row comes from one of two places:

* ``events`` (+ ``event_reviews``)  everything still on the site, reviewed or not
* ``archived_events``               verdicts kept after the images were downloaded and removed,
                                    plus rows imported from a spreadsheet

Both are read, so the numbers do not change when a classified zip is downloaded and its events are
removed, and they never depend on how many events have been reviewed.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ArchivedEvent, Event, EventReview, Site
from app.services.analytics_core import Row, compute_stats
from app.services.xlsx_export import SheetRow, track_from_case_id

RowsBySiteFeature = dict[tuple[int, str], list[Row]]


def load_rows(db: Session, site_id: int | None = None) -> RowsBySiteFeature:
    rows: RowsBySiteFeature = defaultdict(list)

    live = (
        select(
            Event.site_id, Event.feature, EventReview.human_label, EventReview.image_clarity_clear,
            EventReview.make_correct, EventReview.model_correct, EventReview.color_correct,
        )
        .select_from(Event)
        .outerjoin(EventReview, EventReview.event_id == Event.id)
    )
    archived = select(
        ArchivedEvent.site_id, ArchivedEvent.feature, ArchivedEvent.human_label,
        ArchivedEvent.image_clarity_clear, ArchivedEvent.make_correct,
        ArchivedEvent.model_correct, ArchivedEvent.color_correct,
    )
    if site_id is not None:
        live = live.where(Event.site_id == site_id)
        archived = archived.where(ArchivedEvent.site_id == site_id)

    for sid, feature, label, clear, make, model, color in db.execute(live):
        rows[(sid, feature)].append(Row(label, clear, make, model, color, archived=False))
    for sid, feature, label, clear, make, model, color in db.execute(archived):
        rows[(sid, feature)].append(Row(label, clear, make, model, color, archived=True))
    return rows


def analytics_for_sites(db: Session, site_id: int | None = None) -> list[dict[str, Any]]:
    """One entry per site (also sites with no events yet): overall figures and one block per feature."""
    stmt = select(Site).order_by(Site.name, Site.id)
    if site_id is not None:
        stmt = stmt.where(Site.id == site_id)
    sites = list(db.scalars(stmt))
    rows = load_rows(db, site_id)

    out: list[dict[str, Any]] = []
    for site in sites:
        per_feature = {feature: r for (sid, feature), r in rows.items() if sid == site.id}
        everything = [row for r in per_feature.values() for row in r]
        out.append({
            "site_id": site.id,
            "site_name": site.name,
            "site_code": site.code,
            "overall": compute_stats(everything),
            "features": [
                {"feature": feature, "stats": compute_stats(r)}
                for feature, r in sorted(per_feature.items())
            ],
        })
    return out


def export_rows(db: Session, site_id: int, feature: str | None = None) -> dict[str, list[SheetRow]]:
    """Every event of a site as workbook rows, per feature: events still on the site (reviewed or
    not) plus the archived / imported ones."""
    out: dict[str, list[SheetRow]] = defaultdict(list)

    live = (
        select(Event, EventReview)
        .outerjoin(EventReview, EventReview.event_id == Event.id)
        .where(Event.site_id == site_id)
    )
    archived = select(ArchivedEvent).where(ArchivedEvent.site_id == site_id)
    if feature:
        live = live.where(Event.feature == feature)
        archived = archived.where(ArchivedEvent.feature == feature)

    for event, review in db.execute(live):
        out[event.feature].append(SheetRow(
            track_id=track_from_case_id(event.case_id), vrm=event.vrm, timestamp=event.offence_timestamp,
            day_night=event.day_night, label=review.human_label if review else None,
            vrm_correct=review.vrm_correct if review else None,
            clear=review.image_clarity_clear if review else None,
            make=event.vehicle_make, make_conf=event.make_confidence,
            color=event.vehicle_color, color_conf=event.color_confidence,
            model=event.vehicle_model, model_conf=event.model_confidence,
            make_ok=review.make_correct if review else None,
            model_ok=review.model_correct if review else None,
            color_ok=review.color_correct if review else None,
        ))
    for record in db.scalars(archived):
        out[record.feature].append(SheetRow(
            track_id=record.track_id or track_from_case_id(record.case_id), vrm=record.vrm,
            timestamp=record.offence_timestamp, day_night=record.day_night, label=record.human_label,
            issue=record.issue, vrm_correct=record.vrm_correct, clear=record.image_clarity_clear,
            make=record.vehicle_make, make_conf=record.make_confidence,
            color=record.vehicle_color, color_conf=record.color_confidence,
            model=record.vehicle_model, model_conf=record.model_confidence,
            make_ok=record.make_correct, model_ok=record.model_correct, color_ok=record.color_correct,
        ))
    return dict(sorted(out.items()))
