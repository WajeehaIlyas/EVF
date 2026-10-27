"""The .xlsx download: layout like the team's workbook, and it reads back with the importer."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

from openpyxl import load_workbook

from app.services.xlsx_export import SheetRow, build_workbook, mmc_status, track_from_case_id
from app.services.xlsx_import import parse_mmc_status, read_workbook


def test_status_wording_round_trips() -> None:
    assert mmc_status(True, True, True) == "all correct"
    assert mmc_status(False, False, False) == "all wrong"
    assert mmc_status(None, None, None) is None
    assert mmc_status(True, None, True) == "make correct, color correct"
    for ticks in [(True, False, True), (False, True, False), (True, True, None), (False, False, True)]:
        status = mmc_status(*ticks)
        assert status is not None
        got = parse_mmc_status(status)
        assert tuple(bool(t) for t in got) == tuple(bool(t) for t in ticks)


def test_track_ids() -> None:
    assert track_from_case_id("1789403042804_864_WA18LXR") == "864"
    assert track_from_case_id("xlsx:1382:20260410000237:LD13XXL") == "1382"
    assert track_from_case_id("some-folder-name") is None


def _rows() -> list[SheetRow]:
    return [
        SheetRow("864", "WA18LXR", datetime(2026, 4, 10, 0, 5, 1), "Night", "FP", clear=False, vrm_correct=False,
                 make="Ford", make_conf=0.9, make_ok=True, model_ok=False, color_ok=False),
        SheetRow("863", "BD51SMR", datetime(2026, 4, 10, 0, 2, 37), "Night", "TP", clear=None, vrm_correct=True),
        SheetRow("865", "ZZ99ZZZ", datetime(2026, 4, 10, 0, 9, 0), "Day", None),   # not reviewed yet
    ]


def test_layout_and_round_trip() -> None:
    data = build_workbook({"seatbelt": _rows(), "mobilephone": _rows()[:2]})
    book = load_workbook(BytesIO(data))
    seat, phone = book["seatbelt"], book["mobilephone"]

    assert seat.freeze_panes == "A7"
    assert [c.value for c in seat[6]][:9] == ["Track_ID", "OCR / Plate", "Date", "Time", "Day / Night",
                                              "Human Label", "Issue", "OCR Correctness", "Image Clarity"]
    assert seat["P6"].value == "MMC Status" and phone["J6"].value is None   # no vehicle columns on mobilephone
    assert seat["M1"].value == "MMC:" and phone["M1"].value is None
    assert seat["B2"].value.startswith("=COUNTIF(F7:F")
    assert seat["A7"].value == 863                    # oldest first, track id as a number
    assert seat["D7"].number_format == "h:mm:ss" and seat["C7"].number_format == "yyyy-mm-dd"
    assert seat["I7"].value == "Clear"                # reviewed and not marked unclear
    assert seat["I8"].value == "Unclear" and seat["I9"].value is None
    assert seat["P8"].value == "make correct" and seat["J8"].value == "Ford" and seat["J7"].value == "N/A"

    again = {sheet.name: sheet for sheet in read_workbook(data)}
    assert [r.label for r in again["seatbelt"].rows] == ["TP", "FP", None]
    fp = again["seatbelt"].rows[1]
    assert (fp.vrm, fp.track_id, fp.clear, fp.vrm_correct, fp.make, fp.make_conf) == ("WA18LXR", "864", False, False, "Ford", 0.9)
    assert (fp.make_correct, fp.model_correct, fp.color_correct) == (True, False, False)
    assert len(again["mobilephone"].rows) == 2
