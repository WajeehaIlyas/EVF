"""The classified zip: folder layout, naming and safety."""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.services.classified_export import ExportItem, build_zip, safe_name


def test_zip_layout(tmp_path: Path) -> None:
    media = tmp_path / "media"
    (media / "s/seatbelt/full").mkdir(parents=True)
    spec = [("TP", True), ("TP", False), ("FP", False), ("FP", None), ("Unsure", False)]
    items = []
    for i, (label, clear) in enumerate(spec):
        (media / f"s/seatbelt/full/{i}.jpg").write_bytes(b"\xff\xd8" + bytes([i]))
        items.append(ExportItem(i, f"case{i}", label, clear, f"/media/s/seatbelt/full/{i}.jpg"))
    items.append(ExportItem(90, "gone", "TP", True, "/media/missing.jpg"))
    items.append(ExportItem(91, "escape", "TP", True, "/media/../../etc/passwd"))

    name = safe_name("Mersey seatbelt.zip")
    result = build_zip(tmp_path / "out.zip", name, items, media, "/media")

    assert result.included == [0, 1, 2, 3, 4]
    assert result.missing == ["gone", "escape"]      # never in the zip, so never removed from the site
    names = set(zipfile.ZipFile(tmp_path / "out.zip").namelist())
    root = "Mersey seatbelt"
    assert {f"{root}/TP/clear/case0.jpg", f"{root}/TP/unclear/case1.jpg", f"{root}/FP/unclear/case2.jpg",
            f"{root}/FP/clear/case3.jpg", f"{root}/Unsure/case4.jpg", f"{root}/labels.csv"} <= names
    assert {f"{root}/TP/clear/", f"{root}/TP/unclear/", f"{root}/FP/clear/", f"{root}/FP/unclear/", f"{root}/Unsure/"} <= names


def test_safe_name() -> None:
    assert safe_name(None) == "violation-classified"
    assert safe_name("../../x/y") == "x_y"
    assert safe_name("batch 1.zip") == "batch 1"
