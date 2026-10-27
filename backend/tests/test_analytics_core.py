"""The analysis maths and the spreadsheet reader (no database needed)."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.services.analytics_core import Row, compute_stats
from app.services.xlsx_import import parse_mmc_status, read_workbook


def test_numbers_do_not_depend_on_reviewing_everything() -> None:
    rows = [
        Row("TP", clear=True, make=True, model=True, color=False),
        Row("TP", clear=None),
        Row("FP", clear=False),                 # unclear FP
        Row("FP", clear=True),
        Row("FP", clear=None),                  # clarity skipped: not an "unclear" FP
        Row("Unsure", clear=False),
        Row(None),                              # still waiting
        Row(None),
        Row("FP", clear=False, make=False, model=False, color=False, archived=True),
    ]
    s = compute_stats(rows)
    assert (s["total_violations"], s["reviewed"], s["pending"], s["archived"]) == (9, 7, 2, 1)
    assert (s["tp"], s["fp"], s["unsure"]) == (2, 4, 1)
    assert (s["unclear_images"], s["clear_images"]) == (3, 4)
    assert (s["fp_unclear"], s["fp_clear"]) == (2, 2)
    assert s["precision"] == pytest.approx(2 / 6)
    assert s["precision_excl_unclear_fp"] == pytest.approx(2 / 4)
    assert s["mmc_reviewed"] == 2
    assert (s["make_accuracy"], s["model_accuracy"], s["color_accuracy"]) == (0.5, 0.5, 0.0)


def test_empty_gives_dashes_not_errors() -> None:
    s = compute_stats([])
    assert s["total_violations"] == 0
    assert s["precision"] is None and s["make_accuracy"] is None


def test_mmc_status_text() -> None:
    assert parse_mmc_status("all correct") == (True, True, True)
    assert parse_mmc_status("All wrong") == (False, False, False)
    assert parse_mmc_status("make correct") == (True, False, False)
    assert parse_mmc_status("make & colour correct") == (True, False, True)
    assert parse_mmc_status("model correct, color wrong") == (False, True, False)
    assert parse_mmc_status("") == (None, None, None)


def _workbook() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "seatbelt"
    ws.append(["Total Violations:", "=SUBTOTAL(103, A7:A1000)"])  # summary block is ignored
    for _ in range(4):
        ws.append([])
    ws.append(["Track_ID", "OCR / Plate", "Date", "Time", "Day / Night", "Human Label", "Issue",
               "OCR Correctness", "Image Clarity", "MMC Status"])
    ws.append([1382.0, "LD13 XXL", datetime(2026, 4, 10), "00:02:37", "Night", "TP", None, "Correct", "Clear", "all correct"])
    ws.append([1383.0, "YD69XYX", datetime(2026, 4, 10), "00:02:47", "Night", "FP", None, None, "Unclear", None])
    ws.append([1384.0, "AB12CDE", datetime(2026, 4, 10), "00:03:00", "Day", "unsure", None, None, None, None])
    ws.append([1385.0, "ZZ99ZZZ", datetime(2026, 4, 10), "00:04:00", "Day", None, None, None, None, None])   # not labelled yet
    ws.append([1386.0, "QQ11QQQ", datetime(2026, 4, 10), "00:05:00", "Day", "maybe", None, None, None, None])  # bad label
    ws.append([1382.0, "LD13XXL", datetime(2026, 4, 10), "00:02:37", "Night", "TP", None, None, "Clear", None])  # duplicate
    other = wb.create_sheet("Notes")
    other.append(["nothing", "useful"])
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_reads_the_team_workbook_layout() -> None:
    seat, notes = read_workbook(_workbook())
    assert seat.error is None and (seat.unlabelled, seat.invalid, seat.duplicates) == (1, 1, 1)
    assert [r.label for r in seat.rows] == ["TP", "FP", "Unsure", None]
    first = seat.rows[0]
    assert first.vrm == "LD13XXL" and first.timestamp == datetime(2026, 4, 10, 0, 2, 37)
    assert first.key == "xlsx:1382:20260410000237:LD13XXL"
    assert (first.clear, first.vrm_correct, first.make_correct) == (True, True, True)
    assert seat.rows[1].clear is False
    assert notes.error is not None  # no "Human Label" header on that sheet
    stats = compute_stats(Row(r.label, r.clear, r.make_correct, r.model_correct, r.color_correct, True) for r in seat.rows)
    assert (stats["total_violations"], stats["reviewed"], stats["pending"]) == (4, 3, 1)
