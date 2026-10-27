"""End-to-end: LocalFetcher -> PackageParser -> DB (SQLite)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Event
from app.services.fetchers.base import FetcherConfigError
from app.services.fetchers.local_fetcher import LocalFetcher
from app.services.fetchers.sftp_fetcher import SftpConfig, SftpFetcher
from app.services.parser import PackageParser

Maker = Callable[..., Path]


def _run(db: Session, settings: Settings, source: Path, feature: str = "seatbelt"):
    result = LocalFetcher(1, feature, source, settings=settings).fetch()
    stats = PackageParser(db, settings=settings).ingest_fetch_result(result)
    return result, stats


def test_happy_path(db: Session, settings: Settings, tmp_path: Path, make_package: Maker) -> None:
    src = tmp_path / "incoming"
    make_package(  # sibling-image fallback, nested MMC, percent confidence, messy VRM
        src, "C1", vrm=" ab12 cde ", make={"value": "Ford", "confidence": 93},
        model={"value": "Focus", "confidence": 0.88}, color="Blue", color_confidence=0.97,
    )
    make_package(src, "C2", images="explicit", timestamp="10/04/2026 12:00:00 +01:00")

    result, stats = _run(db, settings, src)

    assert result.downloaded == 6 and result.failed == 0          # 2 json + 4 images
    assert stats.events_created == 2 and stats.invalid_records == 0

    c1 = db.scalars(select(Event).where(Event.case_id == "C1")).one()
    assert c1.day_night == "Night" and c1.vrm == "AB12 CDE"
    assert c1.make_confidence == pytest.approx(0.93)
    assert c1.vehicle_color == "Blue" and c1.color_confidence == pytest.approx(0.97)
    assert c1.full_image_url == "/media/events/1/seatbelt/C1_full.jpg"
    assert (settings.media_root / "events/1/seatbelt/C1_full.jpg").is_file()

    c2 = db.scalars(select(Event).where(Event.case_id == "C2")).one()
    assert c2.day_night == "Day"


def test_second_run_skips_everything(
    db: Session, settings: Settings, tmp_path: Path, make_package: Maker
) -> None:
    src = tmp_path / "incoming"
    make_package(src, "C1")
    _run(db, settings, src)

    result, stats = _run(db, settings, src)

    assert result.downloaded == 0 and result.skipped == 3          # file-level dedupe
    assert stats.events_created == 0 and stats.duplicates_skipped == 1   # DB-level dedupe
    assert db.scalar(select(func.count(Event.id))) == 1


def test_bad_packages_do_not_abort_the_run(
    db: Session, settings: Settings, tmp_path: Path, make_package: Maker
) -> None:
    src = tmp_path / "incoming"
    make_package(src, "GOOD")
    make_package(src, "NOIMG", images="none")
    make_package(src, "BADTS", timestamp="not-a-date")
    (src / "broken.json").write_text("{not json", encoding="utf-8")

    _, stats = _run(db, settings, src)

    assert stats.events_created == 1
    assert stats.invalid_records == 3
    assert len(stats.errors) == 3
    assert db.scalar(select(Event.case_id)) == "GOOD"


def test_unknown_site_and_feature(db: Session, settings: Settings) -> None:
    parser = PackageParser(db, settings=settings)
    with pytest.raises(ValueError):
        parser.ingest_files(999, "seatbelt", [])
    with pytest.raises(ValueError):
        parser.ingest_files(1, "jaywalking", [])


def test_feature_aliases_and_missing_source(settings: Settings, tmp_path: Path) -> None:
    fetcher = LocalFetcher(1, "Mobile Phone", tmp_path, settings=settings)
    assert fetcher.feature == "mobilephone"
    with pytest.raises(FetcherConfigError):
        LocalFetcher(1, "seatbelt", tmp_path / "nope", settings=settings)


def test_sftp_requires_credentials(settings: Settings) -> None:
    cfg = SftpConfig(host="example.org", username="u", remote_path="/out")
    with pytest.raises(FetcherConfigError):
        SftpFetcher(1, "seatbelt", cfg, settings=settings)
