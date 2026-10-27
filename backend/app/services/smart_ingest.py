"""Ingest event data whose JSON and images live in different folders.

Real exports keep the data files and the full-frame images in separate folders, named like

    event_1789403042804_864_WA18LXR_MP_P34miyr_MobilePhone_...

where 1789403042804 is the capture time (epoch), 864 the track id and WA18LXR the number plate.
Those three values form a *key* that ties a JSON file to its images, so nothing else in the names
has to match. The JSON is optional: full images alone are enough to create reviewable events.

Staging layout produced by the Import API::

    <staging>/data/**.json      data files
    <staging>/full/**.jpg|png   full-frame images (what the reviewer looks at)
    <staging>/crop/**.jpg|png   optional crop images

Rules
-----
* case_id is "<epoch>_<track>_<plate>" (parts that exist), the same whether the event came from
  JSON + image or from an image alone, so appending the missing half later never duplicates it.
* The JSON is read tolerantly: keys are matched ignoring case/punctuation at any depth, e.g.
  VehicleBrand / vehicle_make / make.value, VehicleBrandScore / makeConfidence ...
* Capture time: a time or epoch field in the JSON, else the epoch in the file name. Epochs are
  UTC, so they are converted to the site's time zone before the Day/Night label is decided.
* A data file with no matching full image is reported as invalid; a full image with no data file
  becomes an "images only" event; a missing crop falls back to the full image.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import ArchivedEvent, Event
from app.services.day_night import TimestampParseError, analyse_timestamp, classify_day_night
from app.services.parser import _normalise_confidence

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
COMMIT_EVERY = 100
MAX_ISSUES = 20
_MIN_EPOCH_MS = 946_684_800_000      # 2000-01-01
_MAX_EPOCH_MS = 4_102_444_800_000    # 2100-01-01


# ------------------------------------------------------------------------ file-name keys
@dataclass(frozen=True)
class NameKey:
    epoch_ms: int
    track: str | None = None
    plate: str | None = None

    @property
    def case_id(self) -> str:
        return "_".join(p for p in (str(self.epoch_ms), self.track, self.plate) if p)


_EPOCH_TOKEN = re.compile(r"\d{10}|\d{13}")
_TRACK_TOKEN = re.compile(r"\d{1,7}")
# A plate token is UPPER-CASE letters and digits with at least one of each (rejects hashes like P34miyr).
_PLATE_TOKEN = re.compile(r"(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{3,10}")


def parse_name_key(name: str) -> NameKey | None:
    """Read (epoch, track id, plate) from a file name; ``None`` if there is no plausible epoch."""
    tokens = [t for t in re.split(r"[_\-\s.]+", Path(name).stem) if t]
    for index, token in enumerate(tokens):
        if not _EPOCH_TOKEN.fullmatch(token):
            continue
        epoch_ms = int(token) * (1000 if len(token) == 10 else 1)
        if not _MIN_EPOCH_MS <= epoch_ms <= _MAX_EPOCH_MS:
            continue
        has_track = index + 1 < len(tokens) and _TRACK_TOKEN.fullmatch(tokens[index + 1])
        track = tokens[index + 1] if has_track else None
        start = index + (2 if track else 1)
        plate = next((t for t in tokens[start:start + 3] if _PLATE_TOKEN.fullmatch(t)), None)
        return NameKey(epoch_ms, track, plate)
    return None


def _epoch_ms_from_value(value: Any) -> int | None:
    """Epoch seconds or milliseconds, as a number or a digit string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        return None
    digits = len(str(abs(number)))
    if digits == 10:
        number *= 1000
    elif digits != 13:
        return None
    return number if _MIN_EPOCH_MS <= number <= _MAX_EPOCH_MS else None


# ----------------------------------------------------------------------- tolerant JSON reading
def _norm(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


_ALIASES: dict[str, tuple[str, ...]] = {
    "plate": ("vrm", "ocr", "plate", "platenumber", "numberplate", "licenseplate", "licenceplate",
              "registration", "platetext", "ocrplate", "lpr"),
    "track": ("trackid", "trackingid", "track", "trackerid", "trackno", "tracknumber"),
    "time": ("timestamp", "offencetimestamp", "offencetime", "eventtime", "capturetime", "datetime",
             "epoch", "epochms", "time", "date", "createdat", "detectedat"),
    "make": ("vehiclebrand", "vehiclemake", "make", "brand", "manufacturer"),
    "model": ("vehiclemodel", "model"),
    "color": ("vehiclecolor", "vehiclecolour", "color", "colour"),
    "case": ("caseid", "eventid"),
}
_CONFIDENCE_ALIASES: dict[str, tuple[str, ...]] = {
    "make": ("vehiclebrandscore", "vehiclemakescore", "makescore", "brandscore", "makeconfidence",
             "brandconfidence", "vehiclemakeconfidence", "vehiclebrandconfidence"),
    "model": ("vehiclemodelscore", "modelscore", "modelconfidence", "vehiclemodelconfidence"),
    "color": ("vehiclecolorscore", "vehiclecolourscore", "colorscore", "colourscore",
              "colorconfidence", "colourconfidence", "vehiclecolorconfidence"),
}
_INNER_VALUE_KEYS = ("value", "name", "label", "text", "result")
_INNER_CONFIDENCE_KEYS = ("confidence", "score", "conf", "probability")


@dataclass
class Fields:
    plate: str | None = None
    track: str | None = None
    time: Any = None
    make: str | None = None
    make_conf: float | None = None
    model: str | None = None
    model_conf: float | None = None
    color: str | None = None
    color_conf: float | None = None
    case_id: str | None = None
    image_refs: list[tuple[str, str]] = field(default_factory=list)  # (normalised key, file name)


def _walk(node: Any, depth: int = 0):
    """Yield (depth, normalised key, value) for every key, nested dicts up to 6 levels."""
    if depth > 6:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            yield depth, _norm(key), value
            if isinstance(value, (dict, list)):
                yield from _walk(value, depth + 1)
    elif isinstance(node, list):
        for item in node[:3]:
            yield from _walk(item, depth)


def _first(entries: list[tuple[int, str, Any]], aliases: tuple[str, ...]) -> Any:
    """Value of the best-matching alias: shallowest first, then earliest in the alias list."""
    best: tuple[tuple[int, int], Any] | None = None
    for depth, key, value in entries:
        if key not in aliases or value is None or value in ("", [], {}):
            continue
        rank = (depth, aliases.index(key))
        if best is None or rank < best[0]:
            best = (rank, value)
    return None if best is None else best[1]


def _unwrap(value: Any) -> tuple[Any, Any]:
    """('Ford', None) for scalars; {'value': 'Ford', 'confidence': .9} -> ('Ford', .9)."""
    if isinstance(value, dict):
        inner = {_norm(k): v for k, v in value.items()}
        text = next((inner[k] for k in _INNER_VALUE_KEYS if inner.get(k) not in (None, "")), None)
        confidence = next((inner[k] for k in _INNER_CONFIDENCE_KEYS if k in inner), None)
        return (None if isinstance(text, (dict, list)) else text), confidence
    if isinstance(value, list):
        head = value[0] if value else None
        return (None if isinstance(head, (dict, list)) else head), None
    return value, None


def _clean(value: Any, limit: int) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _digits(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return str(int(value))
    text = str(value).strip()
    return text if text.isdigit() else None


def extract_fields(data: Any) -> Fields:
    """Pull the interesting values out of an arbitrary JSON document."""
    entries = list(_walk(data))
    out = Fields()

    out.plate = _clean(_unwrap(_first(entries, _ALIASES["plate"]))[0], 50)
    out.track = _digits(_unwrap(_first(entries, _ALIASES["track"]))[0])
    out.time = _unwrap(_first(entries, _ALIASES["time"]))[0]
    out.case_id = _clean(_unwrap(_first(entries, _ALIASES["case"]))[0], 100)

    for attribute in ("make", "model", "color"):
        value, inner_confidence = _unwrap(_first(entries, _ALIASES[attribute]))
        explicit = _first(entries, _CONFIDENCE_ALIASES[attribute])
        setattr(out, attribute, _clean(value, 100))
        setattr(out, f"{attribute}_conf",
                _normalise_confidence(explicit if explicit is not None else inner_confidence))

    for _depth, key, value in entries:
        if isinstance(value, str) and Path(value.replace("\\", "/")).suffix.lower() in IMAGE_EXTENSIONS:
            out.image_refs.append((key, PurePosixPath(value.replace("\\", "/")).name))
    return out


def _normal_plate(value: str | None) -> str | None:
    return re.sub(r"\s+", "", value).upper() if value else None


# --------------------------------------------------------------------------------- image index
class ImageIndex:
    """Finds an image by file name, by (epoch, track, plate) key, or by identical stem."""

    def __init__(self, paths: list[Path]) -> None:
        self.paths = paths
        self._by_name: dict[str, Path] = {}
        self._by_stem: dict[str, Path] = {}
        self._by_triple: dict[tuple[int, str | None, str | None], Path] = {}
        self._by_pair: dict[tuple[int, str | None], Path] = {}
        by_epoch: dict[int, list[Path]] = defaultdict(list)
        for path in paths:  # sorted, so the first of several look-alikes wins
            self._by_name.setdefault(path.name.lower(), path)
            self._by_stem.setdefault(path.stem.lower(), path)
            key = parse_name_key(path.name)
            if key:
                self._by_triple.setdefault((key.epoch_ms, key.track, key.plate), path)
                self._by_pair.setdefault((key.epoch_ms, key.track), path)
                by_epoch[key.epoch_ms].append(path)
        self._unique_epoch = {e: p[0] for e, p in by_epoch.items() if len(p) == 1}

    def by_name(self, name: str) -> Path | None:
        return self._by_name.get(name.lower())

    def find(self, key: NameKey | None, stem: str | None = None) -> Path | None:
        if key is not None:
            hit = self._by_triple.get((key.epoch_ms, key.track, key.plate))
            if hit is None and key.track is not None:
                hit = self._by_pair.get((key.epoch_ms, key.track))
            if hit is None and key.track is None and key.plate is None:
                hit = self._unique_epoch.get(key.epoch_ms)
            if hit is not None:
                return hit
        return self._by_stem.get(stem.lower()) if stem else None


@dataclass
class IngestOutcome:
    created: int = 0
    duplicates: int = 0
    invalid: int = 0
    from_json: int = 0       # created from a data file (+ its image)
    images_only: int = 0     # created from a full image alone
    issues: list[dict[str, str]] = field(default_factory=list)

    def issue(self, source: str, message: str) -> None:
        if len(self.issues) < MAX_ISSUES:
            self.issues.append({"source": source, "message": message})


def _images_under(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS and not p.name.startswith(".")
    )


# ---------------------------------------------------------------------------------- ingestor
class UploadIngestor:
    """Turns a staging folder (see module docstring) into Event rows."""

    def __init__(self, db: Session, settings: Settings, timezone: str = "UTC") -> None:
        self.db = db
        self.settings = settings
        try:
            self._tz = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("Unknown time zone %r; using UTC", timezone)
            self._tz = ZoneInfo("UTC")
        self._media_root = settings.media_root.resolve()
        self._published: dict[Path, str] = {}
        self._pending = 0

    # ------------------------------------------------------------------------- public
    def run(self, staging: Path, site_id: int, feature: str) -> IngestOutcome:
        outcome = IngestOutcome()
        full = ImageIndex(_images_under(staging / "full"))
        crop = ImageIndex(_images_under(staging / "crop"))
        data_dir = staging / "data"
        data_files = sorted(data_dir.rglob("*.json")) if data_dir.is_dir() else []
        dest_root = self.settings.site_feature_dir(site_id, feature)
        used: set[Path] = set()

        for path in data_files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                outcome.invalid += 1
                outcome.issue(path.name, f"Cannot read JSON: {exc}")
                continue
            records = [r for r in (payload if isinstance(payload, list) else [payload]) if isinstance(r, dict)]
            if not records:
                outcome.invalid += 1
                outcome.issue(path.name, "The JSON file has no objects in it")
                continue
            for record in records:
                self._from_data(path, record, site_id, feature, full, crop, used, staging, dest_root, outcome)

        for image in full.paths:
            if image not in used:
                self._from_image(image, site_id, feature, crop, staging, dest_root, outcome)

        self.db.commit()
        logger.info("Smart ingest: %d new (%d from data, %d images only), %d duplicates, %d invalid",
                    outcome.created, outcome.from_json, outcome.images_only,
                    outcome.duplicates, outcome.invalid)
        return outcome

    # ------------------------------------------------------------------------ internals
    def _from_data(self, path: Path, record: dict[str, Any], site_id: int, feature: str,
                   full: ImageIndex, crop: ImageIndex, used: set[Path], staging: Path,
                   dest_root: Path, outcome: IngestOutcome) -> None:
        fields = extract_fields(record)
        name_key = parse_name_key(path.name)
        epoch = name_key.epoch_ms if name_key else _epoch_ms_from_value(fields.time)
        key: NameKey | None = None
        if epoch is not None:
            key = NameKey(
                epoch,
                (name_key.track if name_key and name_key.track else fields.track),
                (name_key.plate if name_key and name_key.plate else _normal_plate(fields.plate)),
            )

        image = next((p for _k, ref in fields.image_refs if (p := full.by_name(ref))), None)
        if image is None:
            image = full.find(key, path.stem)
        if image is None:
            outcome.invalid += 1
            outcome.issue(path.name, "No matching full image (matched on time, track id and plate from the names)")
            return
        used.add(image)

        moment = self._local_time(fields.time, key)
        if moment is None:
            outcome.invalid += 1
            outcome.issue(path.name, "No capture time in the data or in the file names")
            return

        case_id = key.case_id if key else (fields.case_id or path.stem)
        if self._exists(site_id, feature, case_id):
            outcome.duplicates += 1
            return

        crop_image = next((p for _k, ref in fields.image_refs if (p := crop.by_name(ref))), None) \
            or crop.find(key, path.stem)
        full_url = self._publish(image, "full", staging, dest_root)
        crop_url = self._publish(crop_image, "crop", staging, dest_root) if crop_image else full_url

        self._save(
            Event(
                site_id=site_id, feature=feature, case_id=case_id[:100],
                vrm=_normal_plate(fields.plate) or (key.plate if key else None),
                offence_timestamp=moment, day_night=self._day_night(moment),
                full_image_url=full_url, crop_image_url=crop_url,
                vehicle_make=fields.make, make_confidence=fields.make_conf,
                vehicle_model=fields.model, model_confidence=fields.model_conf,
                vehicle_color=fields.color, color_confidence=fields.color_conf,
            ),
            path.name, "data", outcome,
        )

    def _from_image(self, image: Path, site_id: int, feature: str, crop: ImageIndex,
                    staging: Path, dest_root: Path, outcome: IngestOutcome) -> None:
        key = parse_name_key(image.name)
        if key is None:
            outcome.invalid += 1
            outcome.issue(image.name, "No data file matches this image and its name has no readable time")
            return
        case_id = key.case_id
        if self._exists(site_id, feature, case_id):
            outcome.duplicates += 1
            return
        moment = self._local_time(None, key)
        assert moment is not None
        crop_image = crop.find(key, image.stem)
        full_url = self._publish(image, "full", staging, dest_root)
        crop_url = self._publish(crop_image, "crop", staging, dest_root) if crop_image else full_url
        self._save(
            Event(
                site_id=site_id, feature=feature, case_id=case_id[:100], vrm=key.plate,
                offence_timestamp=moment, day_night=self._day_night(moment),
                full_image_url=full_url, crop_image_url=crop_url,
            ),
            image.name, "image", outcome,
        )

    def _local_time(self, raw: Any, key: NameKey | None) -> datetime | None:
        epoch = _epoch_ms_from_value(raw)
        if epoch is None and isinstance(raw, str) and raw.strip():
            try:
                return analyse_timestamp(raw.strip()).local_naive  # keeps the zone the text states
            except TimestampParseError:
                pass
        if epoch is None and key is not None:
            epoch = key.epoch_ms
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch / 1000, tz=self._tz).replace(tzinfo=None)

    def _day_night(self, moment: datetime) -> str:
        return classify_day_night(
            moment, day_start=self.settings.day_start_hour, night_start=self.settings.night_start_hour
        )

    def _exists(self, site_id: int, feature: str, case_id: str) -> bool:
        """True when the event is on the site already OR was archived after a zip download.

        The archived check stops a removed event from coming back as "new" (and being counted
        twice in the analysis) when the same files are imported again.
        """
        case_id = case_id[:100]
        if self.db.scalar(
            select(Event.id).where(
                Event.site_id == site_id, Event.feature == feature, Event.case_id == case_id
            )
        ) is not None:
            return True
        return self.db.scalar(
            select(ArchivedEvent.id).where(
                ArchivedEvent.site_id == site_id,
                ArchivedEvent.feature == feature,
                ArchivedEvent.case_id == case_id,
            )
        ) is not None

    def _publish(self, source: Path, kind: str, staging: Path, dest_root: Path) -> str:
        """Copy an image into the served media folder (once) and return its URL path."""
        cached = self._published.get(source)
        if cached:
            return cached
        target = dest_root / kind / source.relative_to(staging / kind)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not (target.is_file() and target.stat().st_size == source.stat().st_size):
            temp = target.with_name(target.name + ".part")
            shutil.copy2(source, temp)
            os.replace(temp, target)
        prefix = self.settings.media_url_prefix.rstrip("/")
        url = f"{prefix}/{target.resolve().relative_to(self._media_root).as_posix()}"
        self._published[source] = url
        return url

    def _save(self, event: Event, source: str, origin: str, outcome: IngestOutcome) -> None:
        try:
            with self.db.begin_nested():  # a unique-key race only discards this row
                self.db.add(event)
        except IntegrityError:
            outcome.duplicates += 1
            return
        except SQLAlchemyError as exc:
            outcome.invalid += 1
            outcome.issue(source, f"Database error ({exc.__class__.__name__})")
            return
        outcome.created += 1
        if origin == "data":
            outcome.from_json += 1
        else:
            outcome.images_only += 1
        self._pending += 1
        if self._pending >= COMMIT_EVERY:
            self.db.commit()
            self._pending = 0
