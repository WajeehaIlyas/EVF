"""Analytics maths, metrics endpoint, CSV export and graceful Sheets failure."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Event, EventReview
from app.services.analytics import AnalyticsService, safe_pct
from app.services.sheet_exporter import HEADERS_9, HEADERS_16

API = "/api/v1"


def _add(db: Session, case_id: str, hour: int, label: str | None = None,
         feature: str = "seatbelt", **checks: Any) -> Event:
    event = Event(
        site_id=1, feature=feature, case_id=case_id, vrm="AB12CDE",
        offence_timestamp=datetime(2026, 4, 10, hour, 0, 0),
        day_night="Day" if 7 <= hour < 19 else "Night",
        full_image_url="/media/x.jpg", crop_image_url="/media/y.jpg",
        vehicle_make="Ford", make_confidence=0.9, vehicle_model="Focus",
        model_confidence=0.8, vehicle_color="Blue", color_confidence=0.7,
    )
    db.add(event)
    db.flush()
    if label:
        db.add(EventReview(event_id=event.id, reviewer_email="r@example.com",
                           human_label=label, **checks))
    db.commit()
    return event


@pytest.fixture
def seeded(db: Session) -> Session:
    _add(db, "E1", 10, "TP", vrm_correct=True, make_correct=True, model_correct=False,
         color_correct=True, image_clarity_clear=True)
    _add(db, "E2", 12, "TP", vrm_correct=True, make_correct=True, model_correct=True,
         color_correct=None, image_clarity_clear=False)
    _add(db, "E3", 22, "FP", vrm_correct=False, image_clarity_clear=True)
    _add(db, "E4", 2, "TP", vrm_correct=True, make_correct=False, model_correct=False,
         color_correct=False)
    _add(db, "E5", 23)                                    # never reviewed
    _add(db, "M1", 9, "TP", feature="mobilephone", vrm_correct=True)
    return db


# --------------------------------------------------------------------------- analytics
def test_safe_pct_guards_zero_division() -> None:
    assert safe_pct(1, 0) is None
    assert safe_pct(0, 0) is None
    assert safe_pct(1, -3) is None
    assert safe_pct(0, 5) == 0.0
    assert safe_pct(1, 3) == 33.33


def test_summary_numbers(seeded: Session) -> None:
    s = AnalyticsService(seeded).summary(1, "seatbelt", false_negatives=1)
    assert (s.total_reviewed, s.tp, s.fp, s.unsure, s.decided) == (4, 3, 1, 0, 4)
    assert s.precision_pct == 75.0
    assert s.recall_pct == 75.0 and s.recall_available

    acc = s.accuracy
    assert acc["vrm"].accuracy_pct == 75.0 and acc["vrm"].checked == 4
    assert acc["make"].accuracy_pct == 66.67
    assert acc["model"].accuracy_pct == 33.33
    assert acc["color"].accuracy_pct == 50.0      # the skipped (NULL) check is not counted
    assert acc["image_clarity"].accuracy_pct == 66.67
    assert acc["mmc"].accuracy_pct == 50.0

    assert s.by_day_night.day.total_reviewed == 2
    assert s.by_day_night.day.precision_pct == 100.0
    assert s.by_day_night.night.precision_pct == 50.0


def test_all_features_and_breakdown(seeded: Session) -> None:
    s = AnalyticsService(seeded).summary(1)
    assert (s.total_reviewed, s.tp, s.fp) == (5, 4, 1)
    assert s.precision_pct == 80.0
    assert [f.feature for f in s.by_feature] == ["mobilephone", "seatbelt"]


def test_recall_needs_false_negatives(seeded: Session) -> None:
    svc = AnalyticsService(seeded)
    s = svc.summary(1, "seatbelt")
    assert s.recall_pct is None and not s.recall_available and s.recall_note
    assert svc.summary(1, "seatbelt", false_negatives=0).recall_pct == 100.0
    with pytest.raises(ValueError):
        svc.summary(1, "seatbelt", false_negatives=-1)


def test_empty_selection_has_no_percentages(seeded: Session) -> None:
    s = AnalyticsService(seeded).summary(1, "alpr", false_negatives=0)
    assert s.total_reviewed == 0 and s.precision_pct is None and s.recall_pct is None
    assert all(stat.accuracy_pct is None for stat in s.accuracy.values())


# ------------------------------------------------------------------------------- API
def test_metrics_endpoint(client: Any, seeded: Session) -> None:
    r = client.get(f"{API}/metrics/summary",
                   params={"site_id": 1, "feature": "seatbelt", "false_negatives": 1})
    body = r.json()
    assert r.status_code == 200
    assert body["precision_pct"] == 75.0 and body["recall_pct"] == 75.0
    assert body["total_reviewed"] == 4
    assert client.get(f"{API}/metrics/summary", params={"site_id": 99}).status_code == 404
    assert client.get(f"{API}/metrics/summary",
                      params={"site_id": 1, "feature": "nope"}).status_code == 400


def test_csv_9_and_16_columns(client: Any, seeded: Session) -> None:
    r = client.get(f"{API}/export/csv",
                   params={"site_id": 1, "feature": "seatbelt", "layout": "9-col"})
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert r.content.startswith(b"\xef\xbb\xbf")                    # UTF-8 BOM for Excel
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0] == HEADERS_9 and len(rows) == 5                  # header + 4 reviewed
    e3 = next(row for row in rows if row[0] == "E3")
    assert e3[2] == "10/04/2026" and e3[3] == "22:00:00" and e3[4] == "Night"
    assert "False positive" in e3[6] and "VRM incorrect" in e3[6]
    assert e3[7] == "Incorrect" and e3[8] == "Clear"

    r16 = client.get(f"{API}/export/csv",
                     params={"site_id": 1, "feature": "seatbelt", "layout": "16-col"})
    rows16 = list(csv.reader(io.StringIO(r16.content.decode("utf-8-sig"))))
    assert rows16[0] == HEADERS_16 and len(rows16[0]) == 16
    e1 = next(row for row in rows16 if row[0] == "E1")
    assert e1[9:] == ["Ford", "0.9", "Blue", "0.7", "Focus", "0.8", "Incorrect"]


def test_csv_neutralises_formulas_and_handles_empty(client: Any, seeded: Session) -> None:
    _add(seeded, "=1+1", 5, "TP", vrm_correct=True)
    r = client.get(f"{API}/export/csv", params={"site_id": 1, "feature": "seatbelt"})
    assert "'=1+1" in r.content.decode("utf-8-sig")
    empty = client.get(f"{API}/export/csv", params={"site_id": 1, "feature": "alpr"})
    assert empty.status_code == 404


def test_sheets_export_without_credentials_is_graceful(
    client: Any, seeded: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.chdir(tmp_path)                                     # no ./credentials.json here
    monkeypatch.setattr(get_settings(), "google_service_account_file", None)

    status = client.get(f"{API}/export/status").json()
    assert status["google_sheets_configured"] is False and "CSV" in status["detail"]

    body = {"spreadsheet_id": "1" * 44, "site_id": 1, "feature": "seatbelt", "layout": "9-col"}
    r = client.post(f"{API}/export/google-sheets", json=body)
    assert r.status_code == 503 and "not configured" in r.json()["detail"]

    assert client.post(f"{API}/export/google-sheets",
                       json={**body, "spreadsheet_id": "x"}).status_code == 422
    url = "https://docs.google.com/spreadsheets/d/" + "a" * 44 + "/edit#gid=0"
    assert client.post(f"{API}/export/google-sheets",
                       json={**body, "spreadsheet_id": url}).status_code == 503


def test_summary_reports_import_coverage(seeded: Session) -> None:
    s = AnalyticsService(seeded).summary(1, "seatbelt")
    assert (s.total_events, s.pending_review) == (5, 1)          # 5 imported, 4 reviewed
    assert s.last_ingested_at is not None
    assert AnalyticsService(seeded).summary(1).total_events == 6  # all features
