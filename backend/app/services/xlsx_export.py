"""Write a site's events as an .xlsx laid out like the team's "mersey_events" workbook.

One sheet per feature, named after it:

    rows 1-4   coloured summary block (totals, precision, and for MMC features the MMC rates),
               every figure a live formula over the data below, so editing a label updates it
    row 5      blank
    row 6      headers, frozen together with the summary block (freeze pane at A7)
    row 7 ...  one event per row, oldest first

    Track_ID | OCR / Plate | Date | Time | Day / Night | Human Label | Issue | OCR Correctness |
    Image Clarity | VehicleBrand | VehicleBrandScore | VehicleColor | VehicleColorScore |
    VehicleModel | VehicleModelScore | MMC Status          (the vehicle columns: not on mobilephone)

The file reads back with app.services.xlsx_import, so an export can be edited in Excel and
imported again. Formulas are plain COUNTIF / SUMPRODUCT so they work in Excel, LibreOffice and
Google Sheets alike (they do not react to filters, unlike the SUBTOTAL ones in the original).

No database access in here: the caller hands over plain :class:`SheetRow` records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

COMPACT_FEATURES = {"mobilephone"}     # no vehicle / MMC columns, as in the sample workbook
FIRST_ROW = 7
MIN_LAST_ROW = 1000                    # formulas cover at least this far, so added rows count
FONT = "Arial"


@dataclass
class SheetRow:
    track_id: str | None = None
    vrm: str | None = None
    timestamp: datetime | None = None
    day_night: str | None = None
    label: str | None = None           # TP | FP | Unsure | None (not reviewed)
    issue: str | None = None
    vrm_correct: bool | None = None
    clear: bool | None = None          # False = unclear; anything else on a reviewed row = clear
    make: str | None = None
    make_conf: float | None = None
    color: str | None = None
    color_conf: float | None = None
    model: str | None = None
    model_conf: float | None = None
    make_ok: bool | None = None
    model_ok: bool | None = None
    color_ok: bool | None = None


def mmc_status(make_ok: bool | None, model_ok: bool | None, color_ok: bool | None) -> str | None:
    """Text for the MMC Status column, in the wording the sheet's formulas search for."""
    if make_ok is None and model_ok is None and color_ok is None:
        return None
    if make_ok and model_ok and color_ok:
        return "all correct"
    parts = [f"{name} correct" for name, ok in (("make", make_ok), ("model", model_ok), ("color", color_ok)) if ok]
    return ", ".join(parts) if parts else "all wrong"


def track_from_case_id(case_id: str | None) -> str | None:
    """'1789403042804_864_WA18LXR' -> '864'; 'xlsx:1382:...' -> '1382'; otherwise nothing."""
    if not case_id:
        return None
    if case_id.startswith("xlsx:"):
        value = case_id.split(":")[1]
        return value or None
    parts = case_id.split("_")
    if len(parts) >= 2 and parts[0].isdigit() and len(parts[0]) in (10, 13) and parts[1].isdigit():
        return parts[1]
    return None


def clarity_text(label: str | None, clear: bool | None) -> str | None:
    """Reviewed rows are 'Clear' unless the reviewer marked them unclear (the rule the Analytics page uses)."""
    if label is None:
        return None
    return "Unclear" if clear is False else "Clear"


# ------------------------------------------------------------------------------- layout
def _fill(rgb: str) -> PatternFill:
    return PatternFill("solid", fgColor="FF" + rgb)


def _put(sheet: Worksheet, ref: str, value: object, color: str | None = None, *, bold: bool = False,
         number_format: str | None = None, right: bool = False) -> None:
    cell = sheet[ref]
    cell.value = value
    cell.font = Font(name=FONT, bold=bold)
    if color:
        cell.fill = _fill(color)
    if number_format:
        cell.number_format = number_format
    if right:
        cell.alignment = Alignment(horizontal="right")


def _summary(sheet: Worksheet, feature: str, last: int, mmc: bool) -> None:
    rng = lambda col: f"{col}{FIRST_ROW}:{col}{last}"  # noqa: E731
    label, clarity, mmc_col = rng("F"), rng("I"), rng("P")

    _put(sheet, "A1", "Total Violations:", "C27BA0")
    _put(sheet, "B1", f'=SUMPRODUCT(--(LEN({rng("A")}&{rng("B")}&{rng("C")})>0))', "C27BA0", right=True)
    _put(sheet, "A2", "True Positives (TP):", "B6D7A8")
    _put(sheet, "B2", f'=COUNTIF({label},"TP")', "B6D7A8", right=True)
    _put(sheet, "A3", "False Positives (FP):", "EA9999")
    _put(sheet, "B3", f'=COUNTIF({label},"FP")', "EA9999", right=True)
    _put(sheet, "A4", "Unsure", "C9DAF8")
    _put(sheet, "B4", f'=COUNTIF({label},"Unsure")', "C9DAF8", right=True)

    _put(sheet, "D1", "Labelled Events", "FFE599")
    _put(sheet, "E1", f"=COUNTA({label})", "FFE599", right=True)
    _put(sheet, "D2", "Unclear FP", "FFE599")
    _put(sheet, "E2", f'=COUNTIFS({label},"FP",{clarity},"Unclear")', "FFE599", right=True)
    _put(sheet, "D3", "Clear FP", "FFE599")
    _put(sheet, "E3", f'=COUNTIFS({label},"FP",{clarity},"Clear")', "FFE599", right=True)

    _put(sheet, "G2", "Raw precision", "F9CB9C")
    _put(sheet, "H2", "=IFERROR(B2 / (B2 + B3), 0)", "F9CB9C", number_format="0.00%", right=True)
    name = "Precision excluding Unclear FP & SW Issues" if mmc else \
        "Precision excluding Unclear FalsePositive & Software Issues"
    _put(sheet, "G3", name, "F9CB9C")
    _put(sheet, "H3", "=IFERROR(B2 / (B2 + E3), 0)", "F9CB9C", number_format="0.00%", right=True)

    if not mmc:
        return
    _put(sheet, "J2", f"{feature.capitalize()} Class Score", "D9D2E9")   # a threshold you set; kept from the template
    _put(sheet, "K2", 0.7, "D9D2E9", right=True)

    _put(sheet, "M1", "MMC:", "CFE2F3", bold=True)
    for ref in ("N1", "O1", "P1", "Q1", "O2", "O3", "O4"):
        sheet[ref].fill = _fill("FFFFFF")
    _put(sheet, "M2", "Total Validated Events", "FCE5CD")
    _put(sheet, "N2", f"=COUNTA({mmc_col})", "FCE5CD", number_format="#,##0", right=True)
    _put(sheet, "M3", "All Correct Count", "FCE5CD")
    _put(sheet, "N3", f'=COUNTIF({mmc_col},"*all correct*")', "FCE5CD", number_format="#,##0", right=True)
    _put(sheet, "M4", "All Wrong Count", "FCE5CD")
    _put(sheet, "N4", f'=COUNTIF({mmc_col},"*all wrong*")', "FCE5CD", number_format="#,##0", right=True)
    for row, title, word in ((2, "Make Accuracy Rate", "make"), (3, "Model Accuracy Rate", "model"),
                             (4, "Color Accuracy Rate", "color")):
        _put(sheet, f"P{row}", title, "E6B8AF")
        formula = (f'=IFERROR((COUNTIF({mmc_col},"*{word} correct*")+COUNTIF({mmc_col},"*all correct*"))/N2,0)')
        _put(sheet, f"Q{row}", formula, "E6B8AF", number_format="0.00%", right=True)


HEADERS_COMPACT = ["Track_ID", "OCR / Plate", "Date", "Time", "Day / Night", "Human Label", "Issue",
                   "OCR Correctness", "Image Clarity"]
HEADERS_FULL = HEADERS_COMPACT + ["VehicleBrand", "VehicleBrandScore", "VehicleColor", "VehicleColorScore",
                                  "VehicleModel", "VehicleModelScore", "MMC Status"]
WIDTHS_FULL = {"A": 15.1, "B": 17.2, "C": 14.2, "D": 12, "E": 17.0, "F": 18.6, "G": 28.4, "H": 21.4,
               "I": 18.5, "J": 15.2, "K": 22.8, "L": 14.9, "M": 22.4, "N": 15.5, "O": 23.0, "P": 24}
WIDTHS_COMPACT = {"A": 17.8, "B": 14.0, "C": 14.2, "D": 12, "E": 17.0, "F": 18.6, "G": 46.6, "H": 18.1, "I": 18.5}


def _score(value: float | None) -> float:
    return float(value) if value else 0.0


def _write_sheet(sheet: Worksheet, feature: str, rows: list[SheetRow]) -> None:
    mmc = feature not in COMPACT_FEATURES
    last = max(MIN_LAST_ROW, FIRST_ROW + len(rows) - 1)
    headers = HEADERS_FULL if mmc else HEADERS_COMPACT

    _summary(sheet, feature, last, mmc)
    for index, title in enumerate(headers, start=1):
        cell = sheet.cell(row=6, column=index, value=title)
        cell.font = Font(name=FONT)
        cell.alignment = Alignment(horizontal="left")

    ordered = sorted(rows, key=lambda r: (r.timestamp is None, r.timestamp or datetime.min))
    for offset, row in enumerate(ordered):
        number = FIRST_ROW + offset
        track: int | str | None = int(row.track_id) if row.track_id and row.track_id.isdigit() else row.track_id
        values: list[object] = [
            track, row.vrm,
            row.timestamp.replace(hour=0, minute=0, second=0, microsecond=0) if row.timestamp else None,
            row.timestamp.time().replace(microsecond=0) if row.timestamp else None,
            row.day_night, row.label, row.issue,
            None if row.vrm_correct is None else ("Correct" if row.vrm_correct else "Incorrect"),
            clarity_text(row.label, row.clear),
        ]
        if mmc:
            values += [
                row.make or "N/A", _score(row.make_conf), row.color or "N/A", _score(row.color_conf),
                row.model or "N/A", _score(row.model_conf), mmc_status(row.make_ok, row.model_ok, row.color_ok),
            ]
        for column, value in enumerate(values, start=1):
            if value is None:
                continue
            cell = sheet.cell(row=number, column=column, value=value)
            cell.font = Font(name=FONT)
            if column == 3:
                cell.number_format = "yyyy-mm-dd"
            elif column == 4:
                cell.number_format = "h:mm:ss"

    for number in range(6, FIRST_ROW + len(ordered)):
        sheet.row_dimensions[number].height = 22.5
    for column, width in (WIDTHS_FULL if mmc else WIDTHS_COMPACT).items():
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = f"A{FIRST_ROW}"

    for column, options in (("E", "Night,Day"), ("F", "TP,FP,Unsure"), ("H", "Correct,Incorrect"),
                            ("I", "Clear,Unclear")):
        validation = DataValidation(type="list", formula1=f'"{options}"', allow_blank=True)
        validation.add(f"{column}{FIRST_ROW}:{column}{last}")
        sheet.add_data_validation(validation)


def sheet_title(feature: str) -> str:
    return re.sub(r"[\[\]:*?/\\]", "_", feature)[:31] or "events"


def build_workbook(sheets: dict[str, list[SheetRow]]) -> bytes:
    """``sheets`` maps a feature name to its rows; returns the .xlsx file as bytes."""
    workbook = Workbook()
    workbook.remove(workbook.active)
    for feature, rows in sheets.items():
        _write_sheet(workbook.create_sheet(sheet_title(feature)), feature, rows)
    workbook.calculation.fullCalcOnLoad = True   # Excel computes the formulas when it opens the file
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
