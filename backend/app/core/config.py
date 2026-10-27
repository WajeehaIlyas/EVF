"""Application configuration.

All settings are read from environment variables (or a local ``.env`` file) and
validated once at start-up. Use :func:`get_settings` everywhere instead of
instantiating :class:`Settings` directly so the values are cached.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed runtime configuration for the AEVAF backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ app
    app_name: str = "AEVAF"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    # Externally reachable base URL of this API (e.g. https://aevaf.example.com). Used to turn
    # stored "/media/..." paths into absolute image URLs. Falls back to the request's base URL.
    public_base_url: str | None = None

    # ------------------------------------------------------------- database
    database_url: str = "postgresql+psycopg2://aevaf:aevaf@localhost:5432/aevaf"
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_pool_timeout: int = Field(default=30, ge=1)
    db_echo: bool = False

    # ---------------------------------------------------------------- redis
    redis_url: str = "redis://localhost:6379/0"

    # -------------------------------------------------------------- storage
    # Filesystem root where fetchers place packages: <root>/events/<site_id>/<feature>/
    media_root: Path = Path("/media")
    # URL prefix under which `media_root` is served; stored in Event.*_image_url.
    media_url_prefix: str = "/media"

    # Server-side folders the local-ingest endpoint may read from. When empty, local ingestion
    # is unrestricted in development and disabled in every other environment.
    local_ingest_roots: list[Path] = Field(default_factory=list)

    # ----------------------------------------------------------------- SFTP
    # Optional defaults; individual ingestion requests may override them.
    sftp_default_username: str | None = None
    sftp_default_password: SecretStr | None = None
    sftp_default_key_path: Path | None = None
    sftp_connect_timeout: float = Field(default=20.0, gt=0)
    sftp_connect_retries: int = Field(default=3, ge=1)
    # known_hosts file used for host-key verification (strongly recommended).
    sftp_known_hosts_file: Path | None = None
    # INSECURE: auto-accept unknown host keys. Keep False outside local dev.
    sftp_allow_unknown_hosts: bool = False

    # -------------------------------------------------------- Google Sheets
    google_service_account_file: Path | None = None

    # ---------------------------------------------------------- Celery / Redis
    celery_broker_url: str | None = None      # default: REDIS_URL
    celery_result_backend: str | None = None  # default: REDIS_URL, logical database /1

    # --------------------------------------------------- hot-folder watcher
    # Layout: <watch_root>/<site_code>/<feature>/...package files...
    watch_root: Path | None = None
    watch_settle_seconds: float = Field(default=3.0, gt=0)   # quiet time before a folder is ingested
    watch_use_polling: bool = False                           # needed on NFS/SMB/Docker Desktop mounts
    watch_poll_interval_seconds: float = Field(default=5.0, gt=0)
    watch_scan_interval_minutes: int = Field(default=10, ge=0)  # safety-net rescan; 0 disables

    # ------------------------------------------------------ scheduled SFTP sync
    sftp_default_host: str | None = None
    sftp_default_port: int = Field(default=22, ge=1, le=65535)
    # Remote folder for a site/feature; placeholders: {site_code} {site_id} {feature}
    sftp_remote_path_template: str = "/{site_code}/{feature}"
    sftp_sync_targets: str = ""                # "1:seatbelt,2:alpr" (site_id:feature pairs)
    sftp_sync_interval_minutes: int = Field(default=15, ge=0)  # 0 disables the schedule

    # ------------------------------------------------------------ housekeeping
    cleanup_hour_utc: int = Field(default=3, ge=0, le=23)

    # ----------------------------------------------------------- browser uploads
    upload_max_file_mb: int = Field(default=30, ge=1)      # per file
    upload_max_import_gb: int = Field(default=5, ge=1)     # per import (all batches)

    # ------------------------------------------------------------ day/night
    day_start_hour: int = Field(default=7, ge=0, le=23)    # Day   = 07:00-18:59
    night_start_hour: int = Field(default=19, ge=1, le=23)  # Night = 19:00-06:59

    @model_validator(mode="after")
    def _validate_day_night_hours(self) -> "Settings":
        if self.day_start_hour >= self.night_start_hour:
            raise ValueError("day_start_hour must be earlier than night_start_hour")
        return self

    # -------------------------------------------------------------- helpers
    @field_validator(
        "watch_root", "google_service_account_file", "sftp_default_key_path",
        "sftp_known_hosts_file", mode="before",
    )
    @classmethod
    def _blank_path_is_none(cls, value: object) -> object:
        """An unset-but-present env var (``VAR=``) must mean "not configured", not Path('')."""
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def uploads_dir(self) -> Path:
        """Staging area for browser uploads (removed after ingestion)."""
        return self.media_root / "_uploads"

    @property
    def events_dir(self) -> Path:
        """Root directory holding all ingested event packages."""
        return self.media_root / "events"

    def site_feature_dir(self, site_id: int, feature: str) -> Path:
        """Return ``<media_root>/events/<site_id>/<feature>/``."""
        return self.events_dir / str(site_id) / feature


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached :class:`Settings` instance."""
    return Settings()
