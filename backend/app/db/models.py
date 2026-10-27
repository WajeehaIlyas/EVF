"""SQLAlchemy 2.0 ORM models mirroring the EVF PostgreSQL schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TIMESTAMP

from app.core.database import Base


class Site(Base):
    """A physical camera site / deployment that produces event packages."""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.current_timestamp()
    )

    events: Mapped[list[Event]] = relationship(
        back_populates="site", cascade="all, delete-orphan", passive_deletes=True
    )


class Event(Base):
    """A single computer-vision violation event awaiting human validation."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("site_id", "feature", "case_id", name="unique_site_case"),
        CheckConstraint("day_night IN ('Day', 'Night')", name="day_night_valid"),
        Index("ix_events_site_feature_ts", "site_id", "feature", "offence_timestamp"),
        Index("ix_events_vrm", "vrm"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    feature: Mapped[str] = mapped_column(String(50), nullable=False)
    case_id: Mapped[str] = mapped_column(String(100), nullable=False)
    vrm: Mapped[str | None] = mapped_column(String(50))
    # Naive wall-clock time at the camera site (the schema uses TIMESTAMP, not TIMESTAMPTZ).
    offence_timestamp: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    day_night: Mapped[str] = mapped_column(String(10), nullable=False)
    full_image_url: Mapped[str] = mapped_column(Text, nullable=False)
    crop_image_url: Mapped[str] = mapped_column(Text, nullable=False)

    # Extracted Make / Model / Colour (MMC) data and model confidence scores.
    vehicle_make: Mapped[str | None] = mapped_column(String(100))
    make_confidence: Mapped[float | None] = mapped_column(Float)
    vehicle_model: Mapped[str | None] = mapped_column(String(100))
    model_confidence: Mapped[float | None] = mapped_column(Float)
    vehicle_color: Mapped[str | None] = mapped_column(String(50))
    color_confidence: Mapped[float | None] = mapped_column(Float)

    ingested_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.current_timestamp()
    )

    site: Mapped[Site] = relationship(back_populates="events")
    review: Mapped[EventReview | None] = relationship(
        back_populates="event",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class EventReview(Base):
    """The human reviewer's decision for an event (one review per event)."""

    __tablename__ = "event_reviews"
    __table_args__ = (
        CheckConstraint("human_label IN ('TP', 'FP', 'Unsure')", name="human_label_valid"),
        Index("ix_event_reviews_reviewer", "reviewer_email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    reviewer_email: Mapped[str] = mapped_column(String(255), nullable=False)

    # Main decision: 'TP', 'FP' or 'Unsure'.
    human_label: Mapped[str] = mapped_column(String(10), nullable=False)

    # Verification toggles: True = tick, False = cross, None = skipped.
    vrm_correct: Mapped[bool | None] = mapped_column(Boolean, default=None)
    make_correct: Mapped[bool | None] = mapped_column(Boolean, default=None)
    model_correct: Mapped[bool | None] = mapped_column(Boolean, default=None)
    color_correct: Mapped[bool | None] = mapped_column(Boolean, default=None)
    image_clarity_clear: Mapped[bool | None] = mapped_column(Boolean, default=None)

    reviewed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.current_timestamp()
    )

    event: Mapped[Event] = relationship(back_populates="review")


class ImportJob(Base):
    """One browser upload ("import") of event packages into a site/feature.

    Lifecycle: uploading -> queued -> processing -> completed | failed. Appending to an existing
    site simply creates another import for that site; every import is kept as history.
    """

    __tablename__ = "import_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uploading', 'queued', 'processing', 'completed', 'failed')",
            name="import_status_valid",
        ),
        Index("ix_import_jobs_site_created", "site_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # uuid4 hex
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    feature: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="uploading")

    files_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_received: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    events_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_records: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issues: Mapped[list[dict[str, str]] | None] = mapped_column(JSON)  # first few problems
    error: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[str | None] = mapped_column(String(64))
    # time zone of the cameras: epoch timestamps are UTC and Day/Night is decided in local time
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC", server_default="UTC")
    events_from_json: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    events_images_only: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.current_timestamp()
    )
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)


class ArchivedEvent(Base):
    """A reviewed event that no longer lives on the site, kept only for the analysis.

    Two things create these rows:

    * ``source = 'review'`` - the reviewer downloaded the classified zip and chose to remove those
      events. The images and the Event/EventReview rows are deleted; this slim copy of the verdict
      is what keeps the numbers on the Analytics page.
    * ``source = 'xlsx'`` - a spreadsheet imported on the Analytics page. There are no images,
      only the verdicts (and rows without a label yet).

    The (site, feature, case_id) key doubles as a tombstone: the importers skip a case that is
    archived, so deleting an event can never make it come back as "new" on a re-import.
    """

    __tablename__ = "archived_events"
    __table_args__ = (
        UniqueConstraint("site_id", "feature", "case_id", name="unique_archived_case"),
        CheckConstraint(
            "human_label IS NULL OR human_label IN ('TP', 'FP', 'Unsure')",
            name="archived_label_valid",
        ),
        CheckConstraint("source IN ('review', 'xlsx')", name="archived_source_valid"),
        Index("ix_archived_events_site_feature", "site_id", "feature"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    feature: Mapped[str] = mapped_column(String(50), nullable=False)
    case_id: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="review")

    track_id: Mapped[str | None] = mapped_column(String(50))
    vrm: Mapped[str | None] = mapped_column(String(50))
    offence_timestamp: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    day_night: Mapped[str | None] = mapped_column(String(10))

    # What the model saw (kept so the site's .xlsx export still has the vehicle columns)
    vehicle_make: Mapped[str | None] = mapped_column(String(100))
    make_confidence: Mapped[float | None] = mapped_column(Float)
    vehicle_model: Mapped[str | None] = mapped_column(String(100))
    model_confidence: Mapped[float | None] = mapped_column(Float)
    vehicle_color: Mapped[str | None] = mapped_column(String(50))
    color_confidence: Mapped[float | None] = mapped_column(Float)
    issue: Mapped[str | None] = mapped_column(Text)  # the spreadsheet's free "Issue" column

    # NULL only for spreadsheet rows that have no Human Label yet (they still count as violations)
    human_label: Mapped[str | None] = mapped_column(String(10))
    vrm_correct: Mapped[bool | None] = mapped_column(Boolean)
    make_correct: Mapped[bool | None] = mapped_column(Boolean)
    model_correct: Mapped[bool | None] = mapped_column(Boolean)
    color_correct: Mapped[bool | None] = mapped_column(Boolean)
    image_clarity_clear: Mapped[bool | None] = mapped_column(Boolean)  # False = unclear

    archived_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.current_timestamp()
    )


class User(Base):
    """A person who can sign in to the site (username + password only)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, server_default=func.current_timestamp()
    )
