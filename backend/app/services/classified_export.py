"""Build the "classified" zip: reviewed images sorted into folders by the reviewer's verdict.

    <name>/
        TP/clear/      TP/unclear/
        FP/clear/      FP/unclear/
        Unsure/
        labels.csv     one line per image with the verdict and the tick/cross answers

An image counts as unclear only when the reviewer marked it so; an image whose clarity was
skipped goes in ``clear`` (the same rule the Analytics page uses). Unsure images are not split.

No database access in here: the caller hands over plain :class:`ExportItem` records.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

DEFAULT_NAME = "violation-classified"
FOLDERS = ("TP/clear", "TP/unclear", "FP/clear", "FP/unclear", "Unsure")


@dataclass
class ExportItem:
    event_id: int
    case_id: str
    label: str                      # TP | FP | Unsure
    clear: bool | None              # False = unclear
    full_url: str
    crop_url: str | None = None
    vrm: str | None = None
    timestamp: str = ""
    answers: dict[str, bool | None] = field(default_factory=dict)  # vrm/make/model/color/clarity


@dataclass
class ExportResult:
    included: list[int] = field(default_factory=list)        # event ids that are in the zip
    missing: list[str] = field(default_factory=list)         # case ids whose image file was not found
    counts: dict[str, int] = field(default_factory=dict)     # folder -> number of images
    bytes: int = 0


def safe_name(raw: str | None) -> str:
    """A user-typed zip name made safe for a file name and a folder name ('.zip' is optional)."""
    name = (raw or "").strip()
    if name.lower().endswith(".zip"):
        name = name[:-4]
    name = re.sub(r"[^\w.\- ]+", "_", name, flags=re.UNICODE).strip(" ._")[:80]
    return name or DEFAULT_NAME


def folder_for(label: str, clear: bool | None) -> str:
    if label == "Unsure":
        return "Unsure"
    return f"{label}/{'unclear' if clear is False else 'clear'}"


def resolve_media_file(url: str | None, media_root: Path, url_prefix: str) -> Path | None:
    """Map a stored image URL (``/media/site/feature/full/x.jpg``) back to its file, if it is a
    real file inside the media folder."""
    if not url:
        return None
    path = unquote(url.split("?", 1)[0])
    prefix = url_prefix.rstrip("/")
    if prefix and path.startswith(prefix + "/"):
        path = path[len(prefix) + 1:]
    root = media_root.resolve()
    candidate = (root / path.lstrip("/")).resolve()
    if candidate.is_file() and candidate.is_relative_to(root):
        return candidate
    return None


def _file_stem(case_id: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", case_id).strip("._") or "event"


def build_zip(destination: Path, name: str, items: list[ExportItem], media_root: Path,
              url_prefix: str, include_crops: bool = False) -> ExportResult:
    """Write the zip to ``destination``. Events whose full image is missing on disk are left out
    (reported in ``missing``) so the caller never discards something that is not in the file."""
    result = ExportResult(counts={folder: 0 for folder in FOLDERS})
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    used: set[str] = set()
    rows: list[list[str]] = []

    def text(value: bool | None) -> str:
        return "" if value is None else ("yes" if value else "no")

    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for folder in FOLDERS:  # always present, even when empty, so the structure is predictable
            archive.writestr(f"{name}/{folder}/", b"")

        for item in items:
            source = resolve_media_file(item.full_url, media_root, url_prefix)
            if source is None:
                result.missing.append(item.case_id)
                continue
            folder = folder_for(item.label, item.clear)
            stem = _file_stem(item.case_id)
            arcname = f"{name}/{folder}/{stem}{source.suffix.lower()}"
            counter = 2
            while arcname in used:
                arcname = f"{name}/{folder}/{stem}_{counter}{source.suffix.lower()}"
                counter += 1
            used.add(arcname)
            archive.write(source, arcname)
            result.bytes += source.stat().st_size

            if include_crops and item.crop_url and item.crop_url != item.full_url:
                crop = resolve_media_file(item.crop_url, media_root, url_prefix)
                if crop is not None:
                    archive.write(crop, f"{name}/{folder}/{stem}_crop{crop.suffix.lower()}")
                    result.bytes += crop.stat().st_size

            result.included.append(item.event_id)
            result.counts[folder] += 1
            clarity = "" if item.clear is None else ("clear" if item.clear else "unclear")
            rows.append([
                arcname.split("/", 1)[1], item.case_id, item.label, clarity,
                item.vrm or "", item.timestamp,
                text(item.answers.get("vrm")), text(item.answers.get("make")),
                text(item.answers.get("model")), text(item.answers.get("color")),
            ])

        sheet = io.StringIO()
        writer = csv.writer(sheet)
        writer.writerow(["file", "case_id", "label", "clarity", "plate", "time",
                         "plate_correct", "make_correct", "model_correct", "color_correct"])
        writer.writerows(rows)
        archive.writestr(f"{name}/labels.csv", "﻿" + sheet.getvalue(),
                         compress_type=zipfile.ZIP_DEFLATED)

    partial.replace(destination)
    return result
