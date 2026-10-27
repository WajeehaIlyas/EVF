"""Pairing JSON and images from separate folders by the time / track id / plate in their names."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Event
from app.services.smart_ingest import UploadIngestor, extract_fields, parse_name_key

NAME = "event_1789403042804_864_WA18LXR_MP_P34miyr_MobilePhone_1789403042804_864_WA18LXR_MP_P34miyr_MobilePhone"
FLAT = {
    "TrackID": 864, "OCR": "WA18LXR", "Timestamp": 1789403042804,
    "VehicleBrand": "Ford", "VehicleBrandScore": 93, "VehicleColor": "Blue", "VehicleColorScore": 0.8,
    "VehicleModel": "Focus", "VehicleModelScore": 0.7,
}


def test_name_keys() -> None:
    key = parse_name_key(NAME + ".json")
    assert key is not None
    assert (key.epoch_ms, key.track, key.plate) == (1789403042804, "864", "WA18LXR")
    assert key.case_id == "1789403042804_864_WA18LXR"
    assert parse_name_key("event_1789403042_12_AB12CDE_x.jpg").epoch_ms == 1789403042000  # seconds
    assert parse_name_key("random_name.jpg") is None
    # a hash such as P34miyr (lower-case letters) is never mistaken for a plate
    assert parse_name_key("event_1789403042804_864_MP_P34miyr_MobilePhone.jpg").plate is None


def test_tolerant_json_reading() -> None:
    fields = extract_fields(FLAT)
    assert (fields.plate, fields.track, fields.make, fields.model, fields.color) == (
        "WA18LXR", "864", "Ford", "Focus", "Blue")
    assert (fields.make_conf, fields.model_conf, fields.color_conf) == (0.93, 0.7, 0.8)

    nested = {"event": {"vehicle": {"make": {"value": "Audi", "confidence": 0.5}, "colour": "Red"},
                        "plate": {"text": "AB12 CDE"}, "trackId": "12"}}
    parsed = extract_fields(nested)
    assert (parsed.make, parsed.make_conf, parsed.color, parsed.plate, parsed.track) == (
        "Audi", 0.5, "Red", "AB12 CDE", "12")
    assert extract_fields({"nothing": "useful"}).plate is None


def test_pairing_images_only_and_append(db: Session, settings: Settings, tmp_path: Path) -> None:
    staging = tmp_path / "up"
    for sub in ("data", "full", "crop"):
        (staging / sub).mkdir(parents=True)
    data_name = "event_1789403042804_864_WA18LXR_MP_aaa_MobilePhone.json"
    full_name = "event_1789403042804_864_WA18LXR_MobilePhone_zzz.jpg"      # same key, different tail
    image_only = "event_1789403100000_900_BD51SMR_MP_bbb_MobilePhone.jpg"   # no data file
    orphan = "event_1789403200000_901_CD52XYZ_MP_ccc_MobilePhone.json"      # no image
    (staging / "data" / data_name).write_text(json.dumps(FLAT))
    (staging / "data" / "broken.json").write_text("{not json")
    (staging / "data" / orphan).write_text("{}")
    for path in (staging / "full" / full_name, staging / "full" / image_only,
                 staging / "crop" / "event_1789403042804_864_WA18LXR_crop.png"):
        path.write_bytes(b"\xff\xd8x")

    def ingest() -> object:
        return UploadIngestor(db, settings, "Europe/London").run(staging, 1, "mobilephone")

    first = ingest()
    assert (first.created, first.from_json, first.images_only) == (2, 1, 1)
    assert (first.invalid, first.duplicates) == (2, 0)           # broken JSON + orphan data file
    messages = " | ".join(i["message"] for i in first.issues)
    assert "Cannot read JSON" in messages and "No matching full image" in messages

    events = {e.case_id: e for e in db.scalars(select(Event)).all()}
    paired = events["1789403042804_864_WA18LXR"]
    assert paired.vrm == "WA18LXR" and paired.vehicle_make == "Ford" and paired.make_confidence == 0.93
    assert paired.full_image_url.startswith("/media/events/1/mobilephone/full/")
    assert paired.crop_image_url.startswith("/media/events/1/mobilephone/crop/")
    local = datetime.fromtimestamp(1789403042.804, tz=ZoneInfo("Europe/London")).replace(tzinfo=None)
    assert paired.offence_timestamp == local                      # epoch is UTC: shown in site time
    assert paired.day_night == ("Day" if 7 <= local.hour < 19 else "Night")

    lone = events["1789403100000_900_BD51SMR"]
    assert lone.vrm == "BD51SMR" and lone.vehicle_make is None
    assert lone.crop_image_url == lone.full_image_url             # no crop folder: reuse the full image
    assert (settings.site_feature_dir(1, "mobilephone") / "full" / full_name).is_file()

    again = ingest()                                              # appending the same data again
    assert (again.created, again.duplicates) == (0, 2)
