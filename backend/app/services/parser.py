"""Package parser: JSON payloads -> ``Event`` rows.

ASSUMED PACKAGE FORMAT (adjust the key tuples below if your exports differ).
A ``.json`` file holds one record (object) or several (array of objects):

    {
      "case_id": "C-100234",
      "vrm": "AB12 CDE",
      "timestamp": "10/04/2026 05:09:24 +01:00",
      "full_image": "C-100234_full.jpg",        # relative to the JSON file
      "crop_image": "C-100234_crop.jpg",
      "make":  {"value": "Ford",  "confidence": 0.93},   # or flat: "make" + "make_confidence"
      "model": {"value": "Focus", "confidence": 0.88},
      "color": {"value": "Blue",  "confidence": 0.97}
    }

Key lookup is case-insensitive and accepts the aliases listed below. If the image
keys are absent, sibling files named ``<json-stem>*full*`` / ``*crop*`` are used.
Confidences may be 0-1 or 0-100 (values >1 are divided by 100).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import Event, Site
from app.db.schemas import IngestionStats, normalize_feature
from app.services.day_night import TimestampParseError, analyse_timestamp
from app.services.fetchers.base import IMAGE_EXTENSIONS, FetchResult

logger = logging.getLogger(__name__)

CASE_ID_KEYS = ("case_id", "caseid", "case_number", "event_id")
VRM_KEYS = ("vrm", "plate", "plate_number", "license_plate", "registration")
TIMESTAMP_KEYS = ("offence_timestamp", "offence_time", "timestamp", "datetime", "event_time")
FULL_IMAGE_KEYS = ("full_image", "full_image_path", "full_image_url", "overview_image",
                   "scene_image", "image_full")
CROP_IMAGE_KEYS = ("crop_image", "crop_image_path", "crop_image_url", "plate_image", "image_crop")
ATTRIBUTE_ALIASES = {"make": ("make",), "model": ("model",), "color": ("color", "colour")}

COMMIT_BATCH_SIZE = 100


class PackageValidationError(ValueError):
    """A package/record is malformed and cannot be turned into an Event."""


# ------------------------------------------------------------------ small helpers
def _pick(data: Mapping[str, Any], *keys: str) -> Any:
    """Case-insensitive first non-empty value among ``keys``."""
    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None and value != "":
            return value
    return None


def _as_text(value: Any, max_len: int | None = None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len] if max_len else text


def _normalise_confidence(value: Any) -> float | None:
    """Return a 0-1 float, or ``None`` if missing/invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    if number > 1:
        number = number / 100 if number <= 100 else math.nan
    return number if math.isfinite(number) else None


class PackageParser:
    """Reads package JSON files and persists new :class:`Event` records."""

    def __init__(self, db: Session, *, settings: Settings | None = None) -> None:
        self.db = db
        self.settings = settings or get_settings()
        self._media_root = self.settings.media_root.resolve()

    # ------------------------------------------------------------ public API
    def ingest_fetch_result(self, result: FetchResult) -> IngestionStats:
        """Parse every JSON listed in a :class:`FetchResult`."""
        return self.ingest_files(result.site_id, result.feature, result.json_files)

    def ingest_files(
        self, site_id: int, feature: str, json_files: Iterable[Path]
    ) -> IngestionStats:
        """Parse ``json_files`` and insert new events; returns processing statistics.

        Duplicates (same site/feature/case_id) are counted and skipped. Bad files or
        records are reported in the stats and never abort the run.

        Raises:
            ValueError: if the feature is unsupported or the site does not exist.
        """
        feature = normalize_feature(feature)
        if self.db.get(Site, site_id) is None:
            raise ValueError(f"Site {site_id} does not exist")

        stats = IngestionStats()
        pending = 0

        for json_path in json_files:
            stats.json_files_seen += 1
            try:
                records = self._load_records(Path(json_path))
            except PackageValidationError as exc:
                stats.invalid_records += 1
                stats.add_issue(str(json_path), str(exc))
                continue

            for record in records:
                stats.records_seen += 1
                if self._ingest_record(site_id, feature, Path(json_path), record,
                                       len(records) == 1, stats):
                    pending += 1
                if pending >= COMMIT_BATCH_SIZE:
                    self.db.commit()
                    pending = 0

        self.db.commit()
        logger.info(
            "Ingestion done: %d created, %d duplicates, %d invalid (%d records)",
            stats.events_created, stats.duplicates_skipped, stats.invalid_records,
            stats.records_seen,
        )
        return stats

    # ------------------------------------------------------------- internals
    @staticmethod
    def _load_records(json_path: Path) -> list[Mapping[str, Any]]:
        try:
            with json_path.open("r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except (OSError, UnicodeDecodeError) as exc:
            raise PackageValidationError(f"Cannot read file: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PackageValidationError(f"Invalid JSON: {exc}") from exc

        records = payload if isinstance(payload, list) else [payload]
        if not records or not all(isinstance(r, dict) for r in records):
            raise PackageValidationError("JSON must be an object or a non-empty array of objects")
        return records

    def _ingest_record(
        self,
        site_id: int,
        feature: str,
        json_path: Path,
        record: Mapping[str, Any],
        single_record_file: bool,
        stats: IngestionStats,
    ) -> bool:
        """Insert one record. Returns True if a new Event was added."""
        try:
            event = self._build_event(site_id, feature, json_path, record, single_record_file)
        except PackageValidationError as exc:
            stats.invalid_records += 1
            stats.add_issue(str(json_path), str(exc))
            return False

        exists = self.db.scalar(
            select(Event.id).where(
                Event.site_id == site_id,
                Event.feature == feature,
                Event.case_id == event.case_id,
            )
        )
        if exists is not None:
            stats.duplicates_skipped += 1
            return False

        try:
            # SAVEPOINT: a unique-key race only discards this row, not the whole batch.
            with self.db.begin_nested():
                self.db.add(event)
        except IntegrityError:
            stats.duplicates_skipped += 1
            return False
        except SQLAlchemyError as exc:
            stats.invalid_records += 1
            stats.add_issue(str(json_path), f"Database error for case {event.case_id}: {exc}")
            logger.exception("Database error inserting case %s", event.case_id)
            return False

        stats.events_created += 1
        return True

    def _build_event(
        self,
        site_id: int,
        feature: str,
        json_path: Path,
        record: Mapping[str, Any],
        single_record_file: bool,
    ) -> Event:
        case_id = _as_text(_pick(record, *CASE_ID_KEYS), 100)
        if case_id is None and single_record_file:
            case_id = json_path.stem[:100]  # fall back to the file name
        if case_id is None:
            raise PackageValidationError("Missing case_id")

        raw_ts = _as_text(_pick(record, *TIMESTAMP_KEYS))
        if raw_ts is None:
            raise PackageValidationError(f"Case {case_id}: missing offence timestamp")
        try:
            parsed = analyse_timestamp(
                raw_ts,
                day_start=self.settings.day_start_hour,
                night_start=self.settings.night_start_hour,
            )
        except TimestampParseError as exc:
            raise PackageValidationError(f"Case {case_id}: {exc}") from exc

        vrm = _as_text(_pick(record, *VRM_KEYS), 50)
        make, make_conf = self._extract_attribute(record, "make")
        model, model_conf = self._extract_attribute(record, "model")
        color, color_conf = self._extract_attribute(record, "color")

        return Event(
            site_id=site_id,
            feature=feature,
            case_id=case_id,
            vrm=vrm.upper() if vrm else None,
            offence_timestamp=parsed.local_naive,
            day_night=parsed.day_night,
            full_image_url=self._resolve_image(json_path, record, FULL_IMAGE_KEYS, "full", case_id),
            crop_image_url=self._resolve_image(json_path, record, CROP_IMAGE_KEYS, "crop", case_id),
            vehicle_make=make[:100] if make else None,
            make_confidence=make_conf,
            vehicle_model=model[:100] if model else None,
            model_confidence=model_conf,
            vehicle_color=color[:50] if color else None,
            color_confidence=color_conf,
        )

    @staticmethod
    def _extract_attribute(
        record: Mapping[str, Any], attribute: str
    ) -> tuple[str | None, float | None]:
        """Read make/model/colour as nested ``{value, confidence}`` or flat keys."""
        names = ATTRIBUTE_ALIASES[attribute]
        keys = names + tuple(f"vehicle_{n}" for n in names)
        raw = _pick(record, *keys)

        if isinstance(raw, Mapping):
            value = _pick(raw, "value", "name", "label")
            confidence = _pick(raw, "confidence", "score", "conf")
        else:
            value = raw
            confidence = _pick(record, *(f"{k}_confidence" for k in keys))
        return _as_text(value), _normalise_confidence(confidence)

    def _resolve_image(
        self,
        json_path: Path,
        record: Mapping[str, Any],
        keys: tuple[str, ...],
        token: str,
        case_id: str,
    ) -> str:
        """Return the public URL for an image, validating it exists under the media root."""
        raw = _as_text(_pick(record, *keys))
        if raw is not None:
            if raw.lower().startswith(("http://", "https://")):
                return raw
            candidate = json_path.parent / raw
        else:
            sibling = self._find_sibling_image(json_path, token)
            if sibling is None:
                raise PackageValidationError(f"Case {case_id}: no {token} image reference found")
            candidate = sibling
        return self._to_media_url(candidate, case_id)

    @staticmethod
    def _find_sibling_image(json_path: Path, token: str) -> Path | None:
        """Fallback: ``<stem>*<token>*.jpg|png`` next to the JSON file."""
        stem = json_path.stem.lower()
        try:
            for path in sorted(json_path.parent.iterdir()):
                name = path.name.lower()
                if (name.startswith(stem) and token in name
                        and path.suffix.lower() in IMAGE_EXTENSIONS and path.is_file()):
                    return path
        except OSError:
            return None
        return None

    def _to_media_url(self, path: Path, case_id: str) -> str:
        """Convert a file inside ``media_root`` to its served URL; reject anything else."""
        resolved = path.resolve()
        if not resolved.is_file():
            raise PackageValidationError(f"Case {case_id}: image file not found: {path.name}")
        try:
            relative = resolved.relative_to(self._media_root)
        except ValueError as exc:
            raise PackageValidationError(
                f"Case {case_id}: image {path.name} is outside the media root"
            ) from exc
        prefix = self.settings.media_url_prefix.rstrip("/")
        return f"{prefix}/{relative.as_posix()}"
