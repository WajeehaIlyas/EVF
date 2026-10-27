"""API tests: sites, queue/detail, review upsert, static media, WebSocket progress."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app.api.ws.progress import ProgressReporter, job_registry
from app.core.config import get_settings
from app.db.models import Event
from app.services.fetchers.base import TransferProgress

API = "/api/v1"


def _event(db: Session, case_id: str, hour: int = 5, feature: str = "seatbelt") -> Event:
    event = Event(
        site_id=1, feature=feature, case_id=case_id,
        offence_timestamp=datetime(2026, 4, 10, hour, 0, 0), day_night="Night",
        full_image_url=f"/media/events/1/{feature}/{case_id}_full.jpg",
        crop_image_url=f"/media/events/1/{feature}/{case_id}_crop.jpg",
    )
    db.add(event)
    db.commit()
    return event


def test_sites_create_list_duplicate(client: Any) -> None:
    created = client.post(f"{API}/sites", json={"name": "Site B", "code": "B"})
    assert created.status_code == 201
    assert client.post(f"{API}/sites", json={"name": "Dupe", "code": "B"}).status_code == 409
    assert client.post(f"{API}/sites", json={"name": "  ", "code": "X"}).status_code == 400
    codes = [s["code"] for s in client.get(f"{API}/sites").json()]
    assert codes == ["T1", "B"] or sorted(codes) == ["B", "T1"]


def test_queue_review_flow(client: Any, db: Session) -> None:
    first, second = _event(db, "A", hour=5), _event(db, "B", hour=6)
    queue = client.get(f"{API}/events/queue", params={"site_id": 1, "feature": "seatbelt"})
    body = queue.json()
    assert queue.status_code == 200 and body["total"] == 2 and not body["has_more"]
    assert [i["case_id"] for i in body["items"]] == ["A", "B"]          # oldest first
    assert body["items"][0]["full_image_url"] == (
        "http://testserver/media/events/1/seatbelt/A_full.jpg"          # absolute URL
    )

    created = client.post(f"{API}/reviews/", json={
        "event_id": first.id, "human_label": "TP",
        "reviewer_email": "Rev@Example.com", "vrm_correct": True})
    assert created.status_code == 201 and created.json()["status"] == "created"
    assert created.json()["review"]["reviewer_email"] == "rev@example.com"

    updated = client.post(f"{API}/reviews/", json={
        "event_id": first.id, "human_label": "FP",
        "reviewer_email": "rev@example.com", "make_correct": False})
    review = updated.json()["review"]
    assert updated.status_code == 200 and updated.json()["status"] == "updated"
    assert review["human_label"] == "FP"
    assert review["vrm_correct"] is True and review["make_correct"] is False   # omitted = kept

    cleared = client.post(f"{API}/reviews/", json={
        "event_id": first.id, "human_label": "FP",
        "reviewer_email": "rev@example.com", "vrm_correct": None})
    assert cleared.json()["review"]["vrm_correct"] is None                    # null = skipped

    params = {"site_id": 1, "feature": "seatbelt"}
    unreviewed = client.get(f"{API}/events/queue", params=params).json()
    assert unreviewed["total"] == 1 and unreviewed["items"][0]["case_id"] == "B"
    reviewed = client.get(f"{API}/events/queue", params={**params, "status": "reviewed"}).json()
    assert reviewed["total"] == 1 and reviewed["items"][0]["review"]["human_label"] == "FP"

    detail = client.get(f"{API}/events/{second.id}")
    assert detail.status_code == 200 and detail.json()["review"] is None


def test_error_responses(client: Any) -> None:
    q = f"{API}/events/queue"
    assert client.get(q, params={"site_id": 1, "feature": "jaywalking"}).status_code == 400
    assert client.get(q, params={"site_id": 99, "feature": "seatbelt"}).status_code == 404
    assert client.get(f"{API}/events/999").status_code == 404
    bad = {"event_id": 999, "human_label": "TP", "reviewer_email": "a@b.com"}
    assert client.post(f"{API}/reviews/", json=bad).status_code == 404
    bad_label = {"event_id": 1, "human_label": "MAYBE", "reviewer_email": "a@b.com"}
    assert client.post(f"{API}/reviews/", json=bad_label).status_code == 422
    sftp = {"site_id": 1, "feature": "seatbelt", "host": "h", "remote_directory": "/x"}
    assert client.post(f"{API}/ingest/sftp", json=sftp).status_code == 400   # no credentials
    assert client.post(f"{API}/ingest/local", json={
        "site_id": 1, "feature": "seatbelt", "folder_path": "/definitely/not/here"}
    ).status_code == 400


def test_media_is_served(client: Any) -> None:
    target = get_settings().media_root / "events/1/seatbelt/x.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8jpeg")
    assert client.get("/media/events/1/seatbelt/x.jpg").content == b"\xff\xd8jpeg"


def test_ws_progress(client: Any) -> None:
    state = job_registry.create()
    with client.websocket_connect(f"/ws/ingest-progress/{state.job_id}") as ws:
        first = ws.receive_json()                       # snapshot on connect
        assert first["status"] == "queued" and first["percentage"] == 0

        reporter = ProgressReporter(state.job_id)
        reporter.on_transfer(TransferProgress("a.json", 10, 10, 1, 4))
        msg = ws.receive_json()
        assert (msg["processed"], msg["total"], msg["percentage"]) == (1, 4, 25)

        reporter.fail("boom")
        assert ws.receive_json()["status"] == "failed"


def test_ws_unknown_job(client: Any) -> None:
    with client.websocket_connect("/ws/ingest-progress/nope") as ws:
        with pytest.raises(WebSocketDisconnect) as info:
            ws.receive_json()
        assert info.value.code == 4404
