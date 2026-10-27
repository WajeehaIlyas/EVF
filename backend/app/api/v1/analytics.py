"""Analytics: the numbers for every site, and spreadsheet imports that add to them.

    (also mounts the zip-export and Sites-page routes: /analytics/classified-exports/...,
     /analytics/browse/events, /analytics/events/{id}/review, DELETE /analytics/sites/{id})

    GET  /analytics/sites                          figures for every site (and each of its features)
    GET  /analytics/sites/{site_id}/export.xlsx    the site's events as a workbook (one sheet per feature)
    POST /analytics/sites/{site_id}/import-xlsx    upload a labelled .xlsx; its rows join the figures
    GET  /analytics/browse/events                  search the events on a site (Sites page)
    DELETE /analytics/sites/{site_id}              delete a site with everything in it
         /analytics/classified-exports/...         the classified zip (see classified_exports.py)

The figures always include what was archived after a classified-zip download and what was imported
from spreadsheets, so they never shrink when events are removed from the site (see
app.services.analytics). Importing the same workbook again is safe: rows are matched by their
track id / time / plate, so nothing is counted twice and edited labels are updated in place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1 import classified_exports, site_browser
from app.core.database import get_db
from app.db.models import ArchivedEvent, Event, Site
from app.db.schemas import normalize_feature
from app.services.analytics import analytics_for_sites, export_rows
from app.services.xlsx_export import build_workbook
from app.services.xlsx_import import ParsedRow, SheetResult, read_workbook

router = APIRouter(prefix="/analytics", tags=["analytics"])

# One registration is enough: the zip-export and Sites-page routes hang off this router, so they
# can never be "forgotten" when wiring the app. Their URLs all start with /analytics.
router.include_router(classified_exports.router)
router.include_router(site_browser.router)
from app.api.v1 import auth  # noqa: E402

router.include_router(auth.router)

DbSession = Annotated[Session, Depends(get_db)]
SiteId = Annotated[int, PathParam(ge=1)]

MAX_XLSX_BYTES = 25 * 1024 * 1024
READ_CHUNK = 1024 * 1024


class FeatureAnalytics(BaseModel):
    feature: str
    stats: dict[str, Any]


class SiteAnalytics(BaseModel):
    site_id: int
    site_name: str
    site_code: str
    overall: dict[str, Any]
    features: list[FeatureAnalytics]


class SheetImportResult(BaseModel):
    sheet: str
    feature: str | None = None
    rows_read: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    already_on_site: int = 0   # rows for events the site already holds (not counted twice)
    unlabelled: int = 0
    duplicates: int = 0
    invalid: int = 0
    problems: list[str] = Field(default_factory=list)
    skipped_reason: str | None = None


class XlsxImportResult(BaseModel):
    sheets: list[SheetImportResult]
    added: int
    updated: int


@router.get("/sites", response_model=list[SiteAnalytics], summary="Analysis for every site")
def site_analytics(db: DbSession) -> list[dict[str, Any]]:
    return analytics_for_sites(db)


@router.get("/sites/{site_id}/export.xlsx", summary="Download the site's events as an .xlsx")
def export_xlsx(
    site_id: SiteId,
    db: DbSession,
    feature: str | None = None,
) -> Response:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Site {site_id} not found")
    wanted: str | None = None
    if feature and feature.strip():
        wanted = _feature_for_sheet(feature.strip())
        if wanted is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown feature {feature!r}")
    sheets = export_rows(db, site_id, wanted)
    if not sheets:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This site has no events to export yet")
    filename = f"{site.code.lower()}_events.xlsx"
    return Response(
        content=build_workbook(sheets),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _known_moments(db: Session, site_id: int, feature: str) -> set[tuple[str, datetime]]:
    """(plate, time to the second) of every event the site already holds, on screen or archived by
    a zip download, so importing a workbook exported from this site cannot count them twice."""
    pairs = list(db.execute(select(Event.vrm, Event.offence_timestamp).where(
        Event.site_id == site_id, Event.feature == feature)))
    pairs += list(db.execute(select(ArchivedEvent.vrm, ArchivedEvent.offence_timestamp).where(
        ArchivedEvent.site_id == site_id, ArchivedEvent.feature == feature,
        ArchivedEvent.source == "review")))
    return {(vrm.upper(), moment.replace(microsecond=0)) for vrm, moment in pairs if vrm and moment}


def _feature_for_sheet(sheet_name: str) -> str | None:
    try:
        return normalize_feature(sheet_name)
    except Exception:  # noqa: BLE001 - normalize_feature rejects names that are not features
        return None


def _pick_sheets(sheets: list[SheetResult], wanted: str | None) -> list[tuple[SheetResult, str | None, str | None]]:
    """(sheet, feature, reason it is skipped). A sheet is imported into the feature its name says;
    when the user picked a feature and the file has one sheet, that sheet goes there whatever it
    is called."""
    picked: list[tuple[SheetResult, str | None, str | None]] = []
    for sheet in sheets:
        feature = _feature_for_sheet(sheet.name)
        if wanted is not None:
            if feature == wanted or (len(sheets) == 1):
                picked.append((sheet, wanted, None))
            else:
                picked.append((sheet, feature, f"Not the {wanted} sheet"))
        elif feature is None:
            picked.append((sheet, None, "The sheet name is not a feature (use e.g. seatbelt, mobilephone)"))
        else:
            picked.append((sheet, feature, None))
    return picked


def _same(row: ParsedRow, record: ArchivedEvent) -> bool:
    return all(getattr(record, column) == value for column, value in row.columns().items())


def _apply(record: ArchivedEvent, row: ParsedRow) -> None:
    for column, value in row.columns().items():
        setattr(record, column, value)


@router.post(
    "/sites/{site_id}/import-xlsx",
    response_model=XlsxImportResult,
    summary="Add a labelled spreadsheet to a site's analysis",
)
def import_xlsx(
    site_id: SiteId,
    db: DbSession,
    file: Annotated[UploadFile, File(description="An .xlsx with one sheet per feature")],
    feature: Annotated[str | None, Form(description="Only import this feature")] = None,
) -> XlsxImportResult:
    if db.get(Site, site_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Site {site_id} not found")
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please choose an .xlsx file")

    wanted: str | None = None
    if feature and feature.strip():
        wanted = _feature_for_sheet(feature.strip())
        if wanted is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown feature {feature!r}")

    data = bytearray()
    while chunk := file.file.read(READ_CHUNK):
        data += chunk
        if len(data) > MAX_XLSX_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "The spreadsheet is over 25 MB")
    try:
        sheets = read_workbook(bytes(data))
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many types for a bad file
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That file could not be read as an .xlsx") from exc

    results: list[SheetImportResult] = []
    total_added = total_updated = 0
    for sheet, sheet_feature, skipped in _pick_sheets(sheets, wanted):
        result = SheetImportResult(
            sheet=sheet.name, feature=sheet_feature, rows_read=sheet.rows_read,
            unlabelled=sheet.unlabelled, duplicates=sheet.duplicates, invalid=sheet.invalid,
            problems=sheet.problems, skipped_reason=skipped or sheet.error,
        )
        results.append(result)
        if result.skipped_reason or sheet_feature is None:
            continue

        existing = {
            record.case_id: record
            for record in db.scalars(
                select(ArchivedEvent).where(
                    ArchivedEvent.site_id == site_id,
                    ArchivedEvent.feature == sheet_feature,
                    ArchivedEvent.source == "xlsx",
                )
            )
        }
        known = _known_moments(db, site_id, sheet_feature)
        for row in sheet.rows:
            record = existing.get(row.key)
            if record is None and row.vrm and row.timestamp and (row.vrm, row.timestamp) in known:
                result.already_on_site += 1
                continue
            if record is None:
                record = ArchivedEvent(site_id=site_id, feature=sheet_feature, case_id=row.key, source="xlsx")
                _apply(record, row)
                db.add(record)
                result.added += 1
            elif _same(row, record):
                result.unchanged += 1
            else:
                _apply(record, row)
                result.updated += 1
        total_added += result.added
        total_updated += result.updated

    db.commit()
    return XlsxImportResult(sheets=results, added=total_added, updated=total_updated)


# One router to register: the zip export and the Sites page endpoints are mounted here too, so they
# live under /analytics (e.g. /analytics/browse/events, DELETE /analytics/sites/{id},
# /analytics/classified-exports/...). Registering `analytics.router` is all that is needed.
from app.api.v1 import classified_exports, site_browser  # noqa: E402

router.include_router(classified_exports.router)
router.include_router(site_browser.router)
from app.api.v1 import auth  # noqa: E402

router.include_router(auth.router)
