"""Export endpoints: Google Sheets sync and CSV download of the reviewed dataset."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import Site
from app.db.schemas import normalize_feature
from app.services.sheet_exporter import (
    ExportLayout,
    SheetExporter,
    SheetExportError,
    inspect_credentials,
)

router = APIRouter(prefix="/export", tags=["exports"])

DbSession = Annotated[Session, Depends(get_db)]

_URL_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{10,}$")


# -------------------------------------------------------------------------------- models
class GoogleSheetsExportRequest(BaseModel):
    spreadsheet_id: str  # a bare ID or a full Google Sheets URL
    site_id: int = Field(ge=1)
    feature: str | None = None  # omit for all features
    layout: Literal["9-col", "16-col"] = "9-col"
    worksheet_name: str | None = Field(default=None, max_length=100)

    @field_validator("spreadsheet_id")
    @classmethod
    def _extract_spreadsheet_id(cls, value: str) -> str:
        value = value.strip()
        match = _URL_ID.search(value)
        if match:
            return match.group(1)
        if _BARE_ID.match(value):
            return value
        raise ValueError("Provide a spreadsheet ID or a Google Sheets URL")

    @field_validator("feature")
    @classmethod
    def _normalise_feature(cls, value: str | None) -> str | None:
        return normalize_feature(value) if value else None


class GoogleSheetsExportResponse(BaseModel):
    spreadsheet_id: str
    spreadsheet_url: str
    worksheet: str
    rows_written: int
    layout: str
    columns: int


class ExportStatus(BaseModel):
    google_sheets_configured: bool
    service_account_email: str | None = None  # the address to share the spreadsheet with
    detail: str


# ------------------------------------------------------------------------------- helpers
def _require_site(db: Session, site_id: int) -> None:
    if db.get(Site, site_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Site {site_id} not found")


def _http_error(exc: SheetExportError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


# --------------------------------------------------------------------------------- routes
@router.get("/status", response_model=ExportStatus, summary="Is Google Sheets export configured?")
def get_export_status() -> ExportStatus:
    """Lets the UI disable the Sheets button (with an explanation) instead of failing on click."""
    creds = inspect_credentials()
    return ExportStatus(
        google_sheets_configured=creds.configured,
        service_account_email=creds.client_email,
        detail=creds.detail,
    )


@router.post(
    "/google-sheets",
    response_model=GoogleSheetsExportResponse,
    summary="Sync reviewed events to a Google Sheet",
)
def export_to_google_sheets(
    payload: GoogleSheetsExportRequest, db: DbSession
) -> GoogleSheetsExportResponse:
    """Replaces the contents of one worksheet (created if missing) with the reviewed dataset.

    503 = credentials missing/invalid, 404 = unknown site / sheet / nothing reviewed,
    403 = sheet not shared with the service account, 429 = Google rate limit.
    """
    _require_site(db, payload.site_id)
    try:
        result = SheetExporter(db).export_to_sheet(
            spreadsheet_id=payload.spreadsheet_id,
            site_id=payload.site_id,
            feature=payload.feature,
            layout=ExportLayout(payload.layout),
            worksheet_name=payload.worksheet_name,
        )
    except SheetExportError as exc:
        raise _http_error(exc) from exc
    return GoogleSheetsExportResponse(**result.__dict__)


@router.get("/csv", summary="Download the reviewed dataset as CSV")
def export_csv(
    db: DbSession,
    site_id: Annotated[int, Query(ge=1)],
    feature: Annotated[str | None, Query(description="omit for all features")] = None,
    layout: Literal["9-col", "16-col"] = "9-col",
) -> StreamingResponse:
    _require_site(db, site_id)
    feature_key: str | None = None
    if feature:
        try:
            feature_key = normalize_feature(feature)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    try:
        export = SheetExporter(db).to_csv(site_id, feature_key, ExportLayout(layout))
    except SheetExportError as exc:
        raise _http_error(exc) from exc

    return StreamingResponse(
        export.buffer,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            "Cache-Control": "no-store",
        },
    )
