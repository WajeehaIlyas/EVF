"""Export reviewed events to a Google Sheet (gspread) or a downloadable CSV.

Layouts
-------
9-col   Track_ID, OCR / Plate, Date, Time, Day / Night, Human Label, Issue,
        OCR Correctness, Image Clarity
16-col  the 9 above + VehicleBrand, VehicleBrandScore, VehicleColor, VehicleColorScore,
        VehicleModel, VehicleModelScore, MMC Status

Derived columns
---------------
Issue        "False positive" / "Unsure" (from the label) followed by every failed check, e.g.
             "False positive; VRM incorrect; Image unclear". Empty when nothing is wrong.
MMC Status   Incorrect if any of make/model/color was crossed, Correct if at least one was
             ticked and none crossed, Not checked if all three were skipped.

Google credentials come from a service-account key file (``GOOGLE_SERVICE_ACCOUNT_FILE``, or
``credentials.json`` in the working directory). A missing or broken file never crashes the
server: it raises :class:`CredentialsNotConfiguredError`, which the API turns into HTTP 503.
Share the target spreadsheet with the service account's e-mail address (Editor).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import Event, EventReview, Site

logger = logging.getLogger(__name__)

DEFAULT_CREDENTIALS_FILE = Path("credentials.json")
MAX_CREDENTIALS_BYTES = 64 * 1024
SHEET_WRITE_CHUNK_ROWS = 2000
MAX_SHEET_CELLS = 5_000_000  # Google's hard limit is 10M cells per spreadsheet, shared by all tabs
SHEETS_HTTP_TIMEOUT_SECONDS = 60

HEADERS_9 = [
    "Track_ID", "OCR / Plate", "Date", "Time", "Day / Night",
    "Human Label", "Issue", "OCR Correctness", "Image Clarity",
]
HEADERS_16 = HEADERS_9 + [
    "VehicleBrand", "VehicleBrandScore", "VehicleColor", "VehicleColorScore",
    "VehicleModel", "VehicleModelScore", "MMC Status",
]


class ExportLayout(str, Enum):
    NINE = "9-col"
    SIXTEEN = "16-col"

    @property
    def headers(self) -> list[str]:
        return list(HEADERS_9 if self is ExportLayout.NINE else HEADERS_16)


# ----------------------------------------------------------------------------- errors
class SheetExportError(Exception):
    """Base class; ``status_code`` is the HTTP status the API should answer with."""

    status_code = 500


class CredentialsNotConfiguredError(SheetExportError):
    status_code = 503


class ExportDataError(SheetExportError):
    """Unknown site, or nothing reviewed yet."""

    status_code = 404


class SpreadsheetAccessError(SheetExportError):
    def __init__(self, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.status_code = status_code


class RateLimitedError(SheetExportError):
    status_code = 429


class UpstreamError(SheetExportError):
    status_code = 502


class ExportTooLargeError(SheetExportError):
    status_code = 413


# ------------------------------------------------------------------------ credentials
@dataclass(frozen=True)
class CredentialsStatus:
    configured: bool
    detail: str
    client_email: str | None = None
    path: Path | None = None


def inspect_credentials(settings: Settings | None = None) -> CredentialsStatus:
    """Check (without contacting Google) that a usable service-account key file exists."""
    settings = settings or get_settings()
    path = settings.google_service_account_file or DEFAULT_CREDENTIALS_FILE

    if not path.is_file():
        return CredentialsStatus(
            False,
            "Google Sheets export is not configured: no service-account credentials file was "
            "found (set GOOGLE_SERVICE_ACCOUNT_FILE or place credentials.json in the backend "
            "folder). CSV export still works.",
        )
    try:
        if path.stat().st_size > MAX_CREDENTIALS_BYTES:
            raise ValueError("file too large")
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CredentialsStatus(False, "The Google credentials file could not be read or is not valid JSON.")

    if (
        not isinstance(info, dict)
        or info.get("type") != "service_account"
        or not info.get("client_email")
        or not info.get("private_key")
    ):
        return CredentialsStatus(False, "The Google credentials file is not a service-account key.")

    return CredentialsStatus(
        True, "Google Sheets export is ready.", client_email=str(info["client_email"]), path=path
    )


# -------------------------------------------------------------------------- row building
def _verdict(value: bool | None, yes: str, no: str) -> str:
    return "Not checked" if value is None else (yes if value else no)


def _score(value: float | None) -> float | str:
    return "" if value is None else round(float(value), 4)


def _issue_text(row: Row[Any]) -> str:
    parts: list[str] = []
    if row.human_label == "FP":
        parts.append("False positive")
    elif row.human_label == "Unsure":
        parts.append("Unsure")
    for flag, text in (
        (row.vrm_correct, "VRM incorrect"),
        (row.make_correct, "Make incorrect"),
        (row.model_correct, "Model incorrect"),
        (row.color_correct, "Color incorrect"),
    ):
        if flag is False:
            parts.append(text)
    if row.image_clarity_clear is False:
        parts.append("Image unclear")
    return "; ".join(parts)


def _mmc_status(row: Row[Any]) -> str:
    answered = [f for f in (row.make_correct, row.model_correct, row.color_correct) if f is not None]
    if not answered:
        return "Not checked"
    return "Correct" if all(answered) else "Incorrect"


def _format_row(row: Row[Any], layout: ExportLayout) -> list[Any]:
    moment: datetime = row.offence_timestamp
    values: list[Any] = [
        row.case_id,
        row.vrm or "",
        moment.strftime("%d/%m/%Y"),
        moment.strftime("%H:%M:%S"),
        row.day_night,
        row.human_label,
        _issue_text(row),
        _verdict(row.vrm_correct, "Correct", "Incorrect"),
        _verdict(row.image_clarity_clear, "Clear", "Unclear"),
    ]
    if layout is ExportLayout.SIXTEEN:
        values += [
            row.vehicle_make or "",
            _score(row.make_confidence),
            row.vehicle_color or "",
            _score(row.color_confidence),
            row.vehicle_model or "",
            _score(row.model_confidence),
            _mmc_status(row),
        ]
    return values


def _csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formula injection: text starting with = + - @ is prefixed with '."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _safe_filename_part(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "export"


@dataclass(frozen=True)
class CsvExport:
    buffer: io.BytesIO
    filename: str
    rows: int


@dataclass(frozen=True)
class SheetExportResult:
    spreadsheet_id: str
    spreadsheet_url: str
    worksheet: str
    rows_written: int
    layout: str
    columns: int


# ------------------------------------------------------------------------------ exporter
class SheetExporter:
    """Builds the export table and writes it to Google Sheets or CSV."""

    def __init__(self, db: Session, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()

    # ---- data -----------------------------------------------------------------------
    def _get_site(self, site_id: int) -> Site:
        site = self.db.get(Site, site_id)
        if site is None:
            raise ExportDataError(f"Site {site_id} not found")
        return site

    def iter_rows(
        self, site_id: int, feature: str | None, layout: ExportLayout
    ) -> Iterator[list[Any]]:
        """Yield one formatted row per *reviewed* event, oldest offence first."""
        stmt = (
            select(
                Event.case_id, Event.vrm, Event.offence_timestamp, Event.day_night,
                Event.vehicle_make, Event.make_confidence,
                Event.vehicle_model, Event.model_confidence,
                Event.vehicle_color, Event.color_confidence,
                EventReview.human_label, EventReview.vrm_correct, EventReview.make_correct,
                EventReview.model_correct, EventReview.color_correct,
                EventReview.image_clarity_clear,
            )
            .select_from(Event)
            .join(EventReview, EventReview.event_id == Event.id)
            .where(Event.site_id == site_id)
            .order_by(Event.offence_timestamp, Event.id)
        )
        if feature:
            stmt = stmt.where(Event.feature == feature)
        for row in self.db.execute(stmt.execution_options(yield_per=1000)):
            yield _format_row(row, layout)

    # ---- CSV --------------------------------------------------------------------------
    def to_csv(self, site_id: int, feature: str | None, layout: ExportLayout) -> CsvExport:
        """Return the reviewed dataset as an in-memory UTF-8 CSV (with BOM so Excel opens it)."""
        site = self._get_site(site_id)

        text = io.StringIO(newline="")
        writer = csv.writer(text, lineterminator="\r\n")
        writer.writerow(layout.headers)
        count = 0
        for values in self.iter_rows(site_id, feature, layout):
            writer.writerow([_csv_safe(v) for v in values])
            count += 1
        if count == 0:
            raise ExportDataError("There are no reviewed events to export for this selection")

        buffer = io.BytesIO(text.getvalue().encode("utf-8-sig"))
        buffer.seek(0)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = "_".join(
            _safe_filename_part(p)
            for p in ("aevaf", site.code, feature or "all", layout.value, stamp)
        ) + ".csv"
        return CsvExport(buffer=buffer, filename=filename, rows=count)

    # ---- Google Sheets ----------------------------------------------------------------
    def export_to_sheet(
        self,
        *,
        spreadsheet_id: str,
        site_id: int,
        feature: str | None,
        layout: ExportLayout,
        worksheet_name: str | None = None,
    ) -> SheetExportResult:
        """Replace one worksheet's contents with the current reviewed dataset (idempotent)."""
        site = self._get_site(site_id)

        status = inspect_credentials(self.settings)
        if not status.configured or status.path is None:
            raise CredentialsNotConfiguredError(status.detail)

        rows = list(self.iter_rows(site_id, feature, layout))
        if not rows:
            raise ExportDataError("There are no reviewed events to export for this selection")
        table: list[list[Any]] = [layout.headers, *rows]
        if len(table) * len(table[0]) > MAX_SHEET_CELLS:
            raise ExportTooLargeError(
                "This export is too large for one Google Sheet; use the CSV download instead"
            )

        title = self._worksheet_title(site.code, feature, layout, worksheet_name)
        try:
            return self._write_sheet(status, spreadsheet_id, title, table, layout)
        except SheetExportError:
            raise
        except Exception as exc:  # noqa: BLE001 - translate every library error into our own
            raise self._translate(exc, status.client_email) from exc

    @staticmethod
    def _worksheet_title(
        site_code: str, feature: str | None, layout: ExportLayout, requested: str | None
    ) -> str:
        base = (requested or f"{site_code} {feature or 'all'} {layout.value}").strip()
        base = re.sub(r"[\[\]:*?/\\]", " ", base)  # characters Google forbids in tab names
        return (base.strip() or "AEVAF export")[:100]

    def _write_sheet(
        self,
        status: CredentialsStatus,
        spreadsheet_id: str,
        title: str,
        table: list[list[Any]],
        layout: ExportLayout,
    ) -> SheetExportResult:
        try:
            import gspread  # imported lazily so a missing package cannot break server start-up
        except ImportError as exc:
            raise SheetExportError("The gspread package is not installed on the server") from exc

        try:
            client = gspread.service_account(filename=str(status.path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load Google credentials: %s", type(exc).__name__)
            raise CredentialsNotConfiguredError(
                "The Google credentials file could not be loaded"
            ) from exc
        set_timeout = getattr(client, "set_timeout", None)
        if callable(set_timeout):
            set_timeout(SHEETS_HTTP_TIMEOUT_SECONDS)

        spreadsheet = client.open_by_key(spreadsheet_id)

        n_rows, n_cols = max(len(table), 2), len(table[0])
        try:
            worksheet = spreadsheet.worksheet(title)
            worksheet.clear()
            worksheet.resize(rows=n_rows, cols=n_cols)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=title, rows=n_rows, cols=n_cols)

        # RAW: values are stored exactly as given, so nothing is ever evaluated as a formula.
        for start in range(0, len(table), SHEET_WRITE_CHUNK_ROWS):
            chunk = table[start:start + SHEET_WRITE_CHUNK_ROWS]
            worksheet.update(values=chunk, range_name=f"A{start + 1}", raw=True)

        try:  # cosmetic only: never fail the export over formatting
            worksheet.freeze(rows=1)
            worksheet.format("1:1", {"textFormat": {"bold": True}})
        except Exception:  # noqa: BLE001
            logger.debug("Header formatting skipped", exc_info=True)

        return SheetExportResult(
            spreadsheet_id=spreadsheet_id,
            spreadsheet_url=spreadsheet.url,
            worksheet=title,
            rows_written=len(table) - 1,
            layout=layout.value,
            columns=n_cols,
        )

    @staticmethod
    def _translate(exc: Exception, service_email: str | None) -> SheetExportError:
        """Map gspread / google-auth / network errors to a safe, helpful SheetExportError."""
        share_hint = f" Share it with {service_email} as an Editor." if service_email else ""
        name = type(exc).__name__

        if name == "SpreadsheetNotFound":
            return SpreadsheetAccessError(
                "Spreadsheet not found, or it is not shared with the service account." + share_hint,
                status_code=404,
            )
        if name == "APIError":
            code = getattr(exc, "code", None) or getattr(
                getattr(exc, "response", None), "status_code", None
            )
            if code == 403:
                return SpreadsheetAccessError(
                    "Google denied access to the spreadsheet." + share_hint, status_code=403
                )
            if code == 404:
                return SpreadsheetAccessError(
                    "Spreadsheet not found." + share_hint, status_code=404
                )
            if code == 429:
                return RateLimitedError(
                    "Google Sheets rate limit reached; wait a minute and try again"
                )
            logger.warning("Google Sheets API error (HTTP %s)", code)
            return UpstreamError(f"Google Sheets returned an error (HTTP {code or 'unknown'})")
        if name == "RefreshError":
            return CredentialsNotConfiguredError(
                "Google rejected the service-account credentials (revoked key or wrong system clock?)"
            )
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)) or "requests" in type(exc).__module__:
            return UpstreamError("Could not reach Google Sheets; check the server's network access")

        logger.exception("Unexpected Google Sheets export failure")
        return SheetExportError("Unexpected error while writing to Google Sheets")
