"""Pydantic v2 schemas: API validation for sites, events, reviews and ingestion."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

# --------------------------------------------------------------------------- features
SUPPORTED_FEATURES: tuple[str, ...] = (
    "seatbelt",
    "mobilephone",
    "alpr",
    "redlight",
    "bannedturn",
    "nearmiss",
)

DayNightLabel = Literal["Day", "Night"]
HumanLabel = Literal["TP", "FP", "Unsure"]


def normalize_feature(value: str) -> str:
    """Normalise a feature name ('Mobile Phone', 'red_light' ...) to its canonical key.

    Raises:
        ValueError: if the value is not one of :data:`SUPPORTED_FEATURES`.
    """
    key = re.sub(r"[\s_\-]+", "", value.strip().lower())
    if key not in SUPPORTED_FEATURES:
        raise ValueError(
            f"Unsupported feature {value!r}; expected one of {', '.join(SUPPORTED_FEATURES)}"
        )
    return key


class _ORMModel(BaseModel):
    """Base for response models that are built from ORM objects."""

    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------------------ sites
class SiteBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    code: str = Field(min_length=1, max_length=100)


class SiteCreate(SiteBase):
    pass


class SiteRead(SiteBase, _ORMModel):
    id: int
    created_at: datetime


# ----------------------------------------------------------------------------- events
class EventBase(BaseModel):
    site_id: int
    feature: str = Field(max_length=50)
    case_id: str = Field(min_length=1, max_length=100)
    vrm: str | None = Field(default=None, max_length=50)
    offence_timestamp: datetime
    day_night: DayNightLabel
    full_image_url: str
    crop_image_url: str
    vehicle_make: str | None = Field(default=None, max_length=100)
    make_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    vehicle_model: str | None = Field(default=None, max_length=100)
    model_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    vehicle_color: str | None = Field(default=None, max_length=50)
    color_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("feature")
    @classmethod
    def _validate_feature(cls, value: str) -> str:
        return normalize_feature(value)


class EventCreate(EventBase):
    pass


class EventRead(EventBase, _ORMModel):
    id: int
    ingested_at: datetime


# ---------------------------------------------------------------------------- reviews
class ReviewBase(BaseModel):
    human_label: HumanLabel
    # True = tick, False = cross, None = skipped.
    vrm_correct: bool | None = None
    make_correct: bool | None = None
    model_correct: bool | None = None
    color_correct: bool | None = None
    image_clarity_clear: bool | None = None


class ReviewCreate(ReviewBase):
    event_id: int
    reviewer_email: EmailStr


class ReviewRead(ReviewBase, _ORMModel):
    id: int
    event_id: int
    reviewer_email: str
    reviewed_at: datetime


# -------------------------------------------------------------------------- ingestion
class _IngestionBase(BaseModel):
    site_id: int
    feature: str

    @field_validator("feature")
    @classmethod
    def _validate_feature(cls, value: str) -> str:
        return normalize_feature(value)


class LocalIngestionRequest(_IngestionBase):
    """Ingest a package folder that already exists on the server's disk."""

    source_type: Literal["local"] = "local"
    path: str = Field(min_length=1)
    recursive: bool = True


class SftpIngestionRequest(_IngestionBase):
    """Ingest packages from a remote host over SSH/SFTP."""

    source_type: Literal["sftp"] = "sftp"
    host: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    username: str | None = None          # falls back to settings.sftp_default_username
    password: SecretStr | None = None    # falls back to settings.sftp_default_password
    private_key_path: str | None = None  # falls back to settings.sftp_default_key_path
    remote_path: str = Field(min_length=1)
    recursive: bool = True


# Discriminated on `source_type`; a ZIP source can be added as a third member later.
IngestionRequest = Annotated[
    LocalIngestionRequest | SftpIngestionRequest, Field(discriminator="source_type")
]


class IngestionIssue(BaseModel):
    """One problem encountered while ingesting (file-level or record-level)."""

    source: str
    message: str


class TransferStats(BaseModel):
    """File-transfer outcome reported by a fetcher."""

    files_downloaded: int = 0
    files_skipped: int = 0   # already present locally with identical size
    files_failed: int = 0
    bytes_transferred: int = 0
    errors: list[str] = Field(default_factory=list)


class IngestionStats(BaseModel):
    """Parsing outcome reported by :class:`app.services.parser.PackageParser`."""

    json_files_seen: int = 0
    records_seen: int = 0
    events_created: int = 0
    duplicates_skipped: int = 0   # already in the DB
    invalid_records: int = 0
    errors: list[IngestionIssue] = Field(default_factory=list)  # capped; see parser

    def add_issue(self, source: str, message: str, *, cap: int = 100) -> None:
        """Record an error message, keeping at most ``cap`` entries."""
        if len(self.errors) < cap:
            self.errors.append(IngestionIssue(source=source, message=message))


class IngestionResponse(BaseModel):
    """Combined result of fetch + parse, returned by the (future) ingestion endpoint."""

    transfer: TransferStats
    parse: IngestionStats


# ------------------------------------------------------------------- Phase 2: API models
class EventDetail(EventRead):
    """Event plus its review (if any); used by the queue and detail endpoints."""

    review: ReviewRead | None = None


class EventQueueResponse(BaseModel):
    items: list[EventDetail]
    total: int            # total rows matching the filters (ignoring limit/offset)
    limit: int
    offset: int
    has_more: bool


ReviewSaveStatus = Literal["created", "updated"]


class ReviewSaveResult(BaseModel):
    """Returned by POST /reviews: whether the review was created or updated, and when."""

    status: ReviewSaveStatus
    event_id: int
    human_label: HumanLabel
    reviewed_at: datetime
    review: ReviewRead


class IngestJobAccepted(BaseModel):
    """Returned (HTTP 202) when a background ingestion job has been queued."""

    job_id: str
    status: Literal["queued"] = "queued"
    ws_path: str          # connect to this path on the API host to stream progress
