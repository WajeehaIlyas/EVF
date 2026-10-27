"""Celery routing/tasks, hot-folder watcher helpers, queue endpoints, same-origin WebSocket."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app.api.ws.progress import job_registry
from app.core.celery_app import _with_db, celery_app
from app.core.config import Settings, get_settings
from app.db.models import Event
from app.services import directory_watcher as dw
from app.tasks import ingestion_tasks as tasks

API = "/api/v1"


@contextmanager
def _no_lock(*_args: Any, **_kwargs: Any):
    yield


# ---------------------------------------------------------------------------- celery
def test_redis_url_helper_and_routing() -> None:
    assert _with_db("redis://redis:6379/0", 1) == "redis://redis:6379/1"
    assert _with_db("redis://h:6379", 1) == "redis://h:6379/1"
    routes = celery_app.conf.task_routes
    assert routes["aevaf.sftp_sync"]["queue"] == "sftp"
    assert routes["aevaf.scheduled_sftp_syncs"]["queue"] == "sftp"
    assert routes["aevaf.scan_hot_folders"]["queue"] == "ingestion"
    assert routes["aevaf.ingest_local_folder"]["queue"] == "ingestion"
    assert routes["aevaf.cleanup_database"]["queue"] == "maintenance"
    assert "database-cleanup" in celery_app.conf.beat_schedule


def test_parse_sftp_targets() -> None:
    parsed = tasks.parse_sftp_targets("1:seatbelt, 2:Mobile Phone ,bad, x:alpr,3:nope,,")
    assert parsed == [(1, "seatbelt"), (2, "mobilephone")]


def test_local_ingest_task_end_to_end(
    db: Session, settings: Settings, tmp_path: Path, make_package: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(tasks, "_exclusive", _no_lock)
    src = tmp_path / "incoming"
    make_package(src, "C1")

    result = tasks.async_ingest_local_folder.apply(args=(str(src), 1, "seatbelt")).get()
    assert result["parse"]["events_created"] == 1 and result["kind"] == "local"
    assert db.scalar(select(func.count(Event.id))) == 1

    again = tasks.async_ingest_local_folder.apply(args=(str(src), 1, "seatbelt")).get()
    assert again["parse"]["events_created"] == 0 and again["parse"]["duplicates_skipped"] == 1


def test_local_ingest_task_refuses_unlisted_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = Settings(media_root=tmp_path / "media", database_url="sqlite://",
                      environment="production")
    monkeypatch.setattr(tasks, "get_settings", lambda: locked)
    with pytest.raises(PermissionError):
        tasks.async_ingest_local_folder.apply(args=(str(tmp_path), 1, "seatbelt")).get()


def test_hot_folder_scan_queues_known_sites_only(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "hot"
    (root / "T1" / "seatbelt").mkdir(parents=True)
    (root / "T1" / "seatbelt" / "a.json").write_text("{}")
    (root / "NOPE" / "seatbelt").mkdir(parents=True)
    (root / "NOPE" / "seatbelt" / "b.json").write_text("{}")
    cfg = Settings(media_root=tmp_path / "media", database_url="sqlite://", watch_root=root)
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tasks, "get_settings", lambda: cfg)
    monkeypatch.setattr(tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(tasks.async_ingest_local_folder, "delay",
                        lambda *args, **kwargs: calls.append(args))

    out = tasks.scan_hot_folders()
    assert out["queued"] == [str(root / "T1" / "seatbelt")]
    assert calls == [(str(root / "T1" / "seatbelt"), 1, "seatbelt")]


def test_cleanup_removes_only_stale_partial_files(
    db: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = settings.events_dir / "1" / "seatbelt"
    folder.mkdir(parents=True)
    old, fresh, keep = folder / "old.json.part", folder / "new.json.part", folder / "x.json"
    for f in (old, fresh, keep):
        f.write_text("x")
    two_days_ago = time.time() - 48 * 3600
    os.utime(old, (two_days_ago, two_days_ago))
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "SessionLocal", lambda: db)

    out = tasks.cleanup_database()
    assert out["stale_partial_files_removed"] == 1 and out["tables_analyzed"] is False
    assert not old.exists() and fresh.exists() and keep.exists()


# ------------------------------------------------------------------------ the watcher
def test_package_file_filter() -> None:
    assert dw.is_package_file("a/b/C1.json") and dw.is_package_file("x.JPG")
    for name in ("x.json.part", ".hidden.json", "notes.txt", "x.jpg.tmp", "x.png~"):
        assert not dw.is_package_file(name)


def test_locate_hot_folder(tmp_path: Path) -> None:
    root = tmp_path
    hit = dw.locate_hot_folder(root, root / "T1" / "Mobile Phone" / "2026" / "a.json")
    assert hit == dw.HotFolder("T1", "mobilephone", root / "T1" / "Mobile Phone")
    assert dw.locate_hot_folder(root, root / "T1" / "a.json") is None          # too shallow
    assert dw.locate_hot_folder(root, root / "T1" / "jaywalking" / "a.json") is None
    assert dw.locate_hot_folder(root, root / ".trash" / "seatbelt" / "a.json") is None
    assert dw.locate_hot_folder(root, Path("/elsewhere/T1/seatbelt/a.json")) is None
    assert dw.locate_hot_folder(root, root / "T1" / "seatbelt", is_directory=True) is not None


def test_discover_hot_folders(tmp_path: Path) -> None:
    (tmp_path / "T1" / "seatbelt" / "day1").mkdir(parents=True)
    (tmp_path / "T1" / "seatbelt" / "day1" / "a.json").write_text("{}")
    (tmp_path / "T1" / "alpr").mkdir()                                  # empty: skipped
    (tmp_path / "T1" / "junk").mkdir()
    (tmp_path / "T1" / "junk" / "a.json").write_text("{}")              # not a feature: skipped
    found = dw.discover_hot_folders(tmp_path)
    assert [(f.site_code, f.feature) for f in found] == [("T1", "seatbelt")]


def test_debouncer_collapses_bursts_and_retries(tmp_path: Path) -> None:
    folder = dw.HotFolder("T1", "seatbelt", tmp_path)
    seen: list[dw.HotFolder] = []
    deb = dw.Debouncer(0.2, seen.append)
    for _ in range(5):
        deb.touch(folder)
        time.sleep(0.05)
    time.sleep(0.6)
    assert seen == [folder]                                             # one dispatch, not five
    deb.close()

    attempts: list[int] = []

    def flaky(_folder: dw.HotFolder) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("broker down")

    retrying = dw.Debouncer(0.1, flaky, retry_seconds=0.2)
    retrying.touch(folder)
    time.sleep(0.8)
    retrying.close()
    assert len(attempts) == 2


# ------------------------------------------------------------------- API additions
def test_async_local_endpoint(
    client: Any, db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "in"
    folder.mkdir()
    monkeypatch.setattr(get_settings(), "local_ingest_roots", [tmp_path])

    class _Queued:
        id = "task-123"

    monkeypatch.setattr(tasks.async_ingest_local_folder, "delay", lambda *a, **k: _Queued())
    body = {"site_id": 1, "feature": "seatbelt", "folder_path": str(folder)}
    r = client.post(f"{API}/ingest/local/async", json=body)
    assert r.status_code == 202 and r.json()["task_id"] == "task-123"

    def _boom(*_a: Any, **_k: Any) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(tasks.async_ingest_local_folder, "delay", _boom)
    assert client.post(f"{API}/ingest/local/async", json=body).status_code == 503

    outside = {**body, "folder_path": "/definitely/not/allowed"}
    assert client.post(f"{API}/ingest/local/async", json=outside).status_code == 400


def test_websocket_accepts_same_origin_only(client: Any) -> None:
    state = job_registry.create()
    url = f"/ws/ingest-progress/{state.job_id}"
    with client.websocket_connect(url, headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json()["status"] == "queued"                  # same host: allowed
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url, headers={"origin": "http://evil.example"}):
            pass
