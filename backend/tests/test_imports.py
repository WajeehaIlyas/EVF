"""Browser imports: create or append to a site, upload batches, ingest, and the refreshed analysis."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.api.v1.imports import parse_staged_path, slugify_code, unique_code
from app.db.models import Site
from app.tasks import ingestion_tasks as tasks

API = "/api/v1"
JPEG = b"\xff\xd8x"


@contextmanager
def _no_lock(*_args: Any, **_kwargs: Any):
    yield


@pytest.fixture
def inline_ingest(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """No broker in tests: the finish endpoint falls back to running the task inline."""
    monkeypatch.setattr(tasks, "SessionLocal", lambda: db)
    monkeypatch.setattr(tasks, "_exclusive", _no_lock)

    def _no_broker(*_a: Any, **_k: Any) -> None:
        raise ConnectionError("no broker in tests")

    monkeypatch.setattr(tasks.async_ingest_upload, "delay", _no_broker)


def _upload(client: Any, import_id: str, items: list[tuple[str, bytes]]) -> Any:
    files = [("files", (path.rsplit("/", 1)[-1], data, "application/octet-stream")) for path, data in items]
    return client.post(f"{API}/imports/{import_id}/files", files=files, data={"paths": [p for p, _ in items]})


def test_code_and_path_helpers(db: Session) -> None:
    assert slugify_code("North Gate #2") == "NORTH-GATE-2"
    assert slugify_code("   ") == "SITE"
    assert slugify_code("x" * 50) == "X" * 20

    role, relative = parse_staged_path("full/day1/a.JPG")
    assert (role, str(relative)) == ("full", "day1/a.JPG")
    assert parse_staged_path("data\\sub\\a.json") == ("data", parse_staged_path("data/sub/a.json")[1])
    assert parse_staged_path("data/a.jpg") is None          # image under data
    assert parse_staged_path("full/a.json") is None         # json under full
    assert parse_staged_path("other/a.jpg") is None         # unknown role
    assert parse_staged_path("full/../../etc/passwd.jpg") is None
    assert parse_staged_path("full/.hidden.jpg") is None
    assert parse_staged_path("a.jpg") is None               # no role

    db.add(Site(name="North", code="NORTH-GATE"))
    db.commit()
    assert unique_code(db, "NORTH-GATE") == "NORTH-GATE-2"


def test_two_folders_then_append_updates_the_analysis(client: Any, inline_ingest: None) -> None:
    data_name = "event_1789403042804_864_WA18LXR_MP_aaa_MobilePhone"
    first = [
        (f"data/{data_name}.json", json.dumps({"VehicleBrand": "Ford", "VehicleBrandScore": 0.9}).encode()),
        ("full/event_1789403042804_864_WA18LXR_MobilePhone_zzz.jpg", JPEG),   # pairs with the JSON
        ("full/event_1789403100000_900_BD51SMR_MP_bbb_MobilePhone.jpg", JPEG),  # image only
    ]
    started = client.post(f"{API}/imports", json={
        "feature": "mobilephone", "new_site": {"name": "North Gate"}, "timezone": "Europe/London"})
    assert started.status_code == 201
    job = started.json()
    assert job["site_code"] == "NORTH-GATE" and job["created_site"] is True
    assert job["status"] == "uploading" and job["timezone"] == "Europe/London"

    uploaded = _upload(client, job["id"], first)
    assert uploaded.json()["received"] == 3 and uploaded.json()["skipped"] == 0

    done = client.post(f"{API}/imports/{job['id']}/finish").json()
    assert done["status"] == "completed"
    assert (done["events_created"], done["events_from_json"], done["events_images_only"]) == (2, 1, 1)
    site_id = done["site_id"]

    # Append: one new image plus one that is already in the site.
    again = client.post(f"{API}/imports", json={"feature": "mobilephone", "site_id": site_id}).json()
    assert again["created_site"] is False and again["site_id"] == site_id
    _upload(client, again["id"], [
        ("full/event_1789403300000_902_EF53ABC_MobilePhone.jpg", JPEG),
        ("full/event_1789403100000_900_BD51SMR_MP_bbb_MobilePhone.jpg", JPEG),
    ])
    appended = client.post(f"{API}/imports/{again['id']}/finish").json()
    assert (appended["events_created"], appended["duplicates_skipped"]) == (1, 1)

    metrics = client.get(f"{API}/metrics/summary",
                         params={"site_id": site_id, "feature": "mobilephone"}).json()
    assert metrics["total_events"] == 3 and metrics["pending_review"] == 3 and metrics["last_ingested_at"]

    stats = {s["id"]: s for s in client.get(f"{API}/sites/stats").json()}
    assert stats[site_id]["event_count"] == 3 and stats[site_id]["pending_count"] == 3

    history = client.get(f"{API}/imports", params={"site_id": site_id}).json()
    assert sorted(h["events_created"] for h in history) == [1, 2]


def test_images_only_import_needs_no_json(client: Any, inline_ingest: None) -> None:
    job = client.post(f"{API}/imports", json={"feature": "seatbelt", "site_id": 1}).json()
    _upload(client, job["id"], [("full/event_1789403042804_864_WA18LXR_Seatbelt.jpg", JPEG)])
    done = client.post(f"{API}/imports/{job['id']}/finish").json()
    assert done["status"] == "completed" and done["events_images_only"] == 1


def test_create_import_validation(client: Any, db: Session) -> None:
    ok = {"feature": "seatbelt"}
    assert client.post(f"{API}/imports", json=ok).status_code == 422                       # no destination
    both = {**ok, "site_id": 1, "new_site": {"name": "X"}}
    assert client.post(f"{API}/imports", json=both).status_code == 422                     # two destinations
    assert client.post(f"{API}/imports", json={**ok, "site_id": 99}).status_code == 404
    assert client.post(f"{API}/imports", json={"feature": "jaywalking", "site_id": 1}).status_code == 422
    assert client.post(f"{API}/imports", json={**ok, "site_id": 1, "timezone": "Mars/Base"}).status_code == 422

    dupe = {**ok, "new_site": {"name": "Other", "code": "T1"}}                              # T1 exists
    assert client.post(f"{API}/imports", json=dupe).status_code == 409
    bad_code = {**ok, "new_site": {"name": "Other", "code": "no spaces!"}}
    assert client.post(f"{API}/imports", json=bad_code).status_code == 400

    auto = client.post(f"{API}/imports", json={**ok, "new_site": {"name": "T1"}}).json()
    assert auto["site_code"] == "T1-2"                                                      # auto code made unique


def test_upload_filters_cancel_and_ids(client: Any, db: Session) -> None:
    job = client.post(f"{API}/imports", json={"feature": "seatbelt", "site_id": 1}).json()
    url = f"{API}/imports/{job['id']}/files"

    items = [
        ("full/notes.txt", b"x"),            # unsupported type
        ("full/.hidden.jpg", JPEG),          # hidden file
        ("full/a.jpg", JPEG),                # ok
        ("data/b.jpg", JPEG),                # image under the data role
        ("crop/c.png", JPEG),                # ok
        ("elsewhere/d.jpg", JPEG),           # unknown role
    ]
    sent = _upload(client, job["id"], items)
    assert sent.json() == {"received": 2, "skipped": 4, "bytes": 6, "rejected": []}

    mismatch = client.post(url, files=[("files", ("a.jpg", JPEG, "image/jpeg"))], data={"paths": ["x", "y"]})
    assert mismatch.status_code == 400

    # only data files uploaded -> nothing to review yet
    other = client.post(f"{API}/imports", json={"feature": "seatbelt", "site_id": 1}).json()
    _upload(client, other["id"], [("data/x.json", b"{}")])
    assert client.post(f"{API}/imports/{other['id']}/finish").status_code == 400

    assert client.delete(f"{API}/imports/{job['id']}").json()["status"] == "failed"
    late = client.post(url, files=[("files", ("a.jpg", JPEG, "image/jpeg"))], data={"paths": ["full/a.jpg"]})
    assert late.status_code == 409

    assert client.get(f"{API}/imports/{'0' * 32}").status_code == 404
    assert client.get(f"{API}/imports/not-an-id").status_code == 422
