"""Shared fixtures: in-memory SQLite DB, temp media root, package builder."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

# Must be set before `app.core.database` is imported (it builds an engine at import time).
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("ENVIRONMENT", "staging")          # skip dev-only init_db() on app start
os.environ.setdefault("MEDIA_ROOT", tempfile.mkdtemp(prefix="aevaf-media-"))

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base
from app.db import models

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(media_root=tmp_path / "media", database_url="sqlite://")


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    # Standard recipe so SAVEPOINTs (begin_nested) work with pysqlite.
    @event.listens_for(engine, "connect")
    def _connect(dbapi_conn: Any, _rec: Any) -> None:
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(conn: Any) -> None:
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    session.add(models.Site(id=1, name="Test Site", code="T1"))
    session.commit()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def make_package() -> Callable[..., Path]:
    """Write ``<case>.json`` (+ optional images) into a folder; returns the JSON path."""

    def _make(
        folder: Path,
        case_id: str,
        *,
        images: str = "sibling",   # "sibling" -> <case>_full/_crop.jpg, "explicit" -> keys, "none"
        **fields: Any,
    ) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {"case_id": case_id, "timestamp": "10/04/2026 05:09:24 +01:00"}
        record.update(fields)
        if images in {"sibling", "explicit"}:
            (folder / f"{case_id}_full.jpg").write_bytes(JPEG)
            (folder / f"{case_id}_crop.jpg").write_bytes(JPEG)
        if images == "explicit":
            record["full_image"] = f"{case_id}_full.jpg"
            record["crop_image"] = f"{case_id}_crop.jpg"
        path = folder / f"{case_id}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    return _make


@pytest.fixture
def client(db: Session) -> Generator[Any, None, None]:
    """FastAPI TestClient wired to the in-memory SQLite session."""
    from fastapi.testclient import TestClient

    from app.core.database import get_db
    from main import app

    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:   # `with` runs lifespan (binds the WS event loop)
        yield test_client
    app.dependency_overrides.clear()
