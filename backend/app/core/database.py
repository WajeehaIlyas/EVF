"""SQLAlchemy engine, session factory and FastAPI dependency."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

settings = get_settings()

# Deterministic constraint names keep future Alembic migrations predictable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base class for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _engine_kwargs() -> dict[str, Any]:
    """Build engine options; SQLite (used in tests) does not support pool sizing."""
    kwargs: dict[str, Any] = {"pool_pre_ping": True, "echo": settings.db_echo}
    if not settings.database_url.startswith("sqlite"):
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
        )
    return kwargs


engine = create_engine(settings.database_url, **_engine_kwargs())

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a session that is always closed afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Transactional scope for scripts and Celery tasks (commit / rollback / close)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


_LIGHT_MIGRATIONS = (
    "ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) NOT NULL DEFAULT 'UTC'",
    "ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS events_from_json INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS events_images_only INTEGER NOT NULL DEFAULT 0",
    # archived_events was added in update 3; these columns came with update 4
    "ALTER TABLE archived_events ADD COLUMN IF NOT EXISTS track_id VARCHAR(50)",
    "ALTER TABLE archived_events ADD COLUMN IF NOT EXISTS vehicle_make VARCHAR(100)",
    "ALTER TABLE archived_events ADD COLUMN IF NOT EXISTS make_confidence DOUBLE PRECISION",
    "ALTER TABLE archived_events ADD COLUMN IF NOT EXISTS vehicle_model VARCHAR(100)",
    "ALTER TABLE archived_events ADD COLUMN IF NOT EXISTS model_confidence DOUBLE PRECISION",
    "ALTER TABLE archived_events ADD COLUMN IF NOT EXISTS vehicle_color VARCHAR(50)",
    "ALTER TABLE archived_events ADD COLUMN IF NOT EXISTS color_confidence DOUBLE PRECISION",
    "ALTER TABLE archived_events ADD COLUMN IF NOT EXISTS issue TEXT",
)


def init_db() -> None:
    """Create all tables. Convenient for dev/tests; use Alembic in production."""
    from app.db import models  # noqa: F401  (import registers the tables on Base.metadata)

    Base.metadata.create_all(bind=engine)

    # create_all never alters existing tables, so columns added later are applied here. Each
    # statement is idempotent. (Replace with Alembic migrations when the schema grows further.)
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            for statement in _LIGHT_MIGRATIONS:
                connection.execute(text(statement))
