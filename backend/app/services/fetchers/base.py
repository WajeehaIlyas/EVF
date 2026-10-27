"""Abstract interface shared by every ingest fetcher (SFTP, local, future ZIP)."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from types import TracebackType

from app.core.config import Settings, get_settings
from app.db.schemas import TransferStats, normalize_feature

logger = logging.getLogger(__name__)

JSON_EXTENSION = ".json"
IMAGE_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png"})
SUPPORTED_EXTENSIONS: frozenset[str] = IMAGE_EXTENSIONS | {JSON_EXTENSION}


class FetcherError(Exception):
    """Base class for fetcher failures."""


class FetcherConfigError(FetcherError):
    """Invalid or missing fetcher configuration (bad path, no credentials ...)."""


class FetcherConnectionError(FetcherError):
    """The remote source could not be reached or authenticated."""


@dataclass(frozen=True)
class TransferProgress:
    """Progress snapshot, suitable for pushing over a WebSocket."""

    filename: str
    bytes_done: int
    bytes_total: int
    files_done: int
    files_total: int


ProgressCallback = Callable[[TransferProgress], None]


@dataclass
class FetchResult:
    """Outcome of one fetch run.

    ``json_files`` lists *every* package JSON present locally for this run
    (newly downloaded and already-present). The parser de-duplicates against the
    database, so a run that crashed after downloading can be safely re-parsed.
    """

    site_id: int
    feature: str
    destination_dir: Path
    json_files: list[Path] = field(default_factory=list)
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_transferred: int = 0
    errors: list[str] = field(default_factory=list)

    def to_stats(self) -> TransferStats:
        return TransferStats(
            files_downloaded=self.downloaded,
            files_skipped=self.skipped,
            files_failed=self.failed,
            bytes_transferred=self.bytes_transferred,
            errors=self.errors[:100],
        )


class BaseFetcher(ABC):
    """Template for fetchers: transfer packages into ``<media>/events/<site_id>/<feature>/``."""

    def __init__(
        self,
        site_id: int,
        feature: str,
        *,
        settings: Settings | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        try:
            self.feature = normalize_feature(feature)
        except ValueError as exc:
            raise FetcherConfigError(str(exc)) from exc
        self.site_id = site_id
        self.settings = settings or get_settings()
        self._progress_callback = progress_callback
        self.destination_dir: Path = self.settings.site_feature_dir(site_id, self.feature)

    # ----------------------------------------------------------- interface
    @abstractmethod
    def fetch(self) -> FetchResult:
        """Transfer all supported files and return a :class:`FetchResult`.

        Implementations must skip files that already exist locally, never raise
        for a single bad file (record it in ``errors``), and raise
        :class:`FetcherError` only when the whole run cannot proceed.
        """

    def close(self) -> None:
        """Release any resources (connections ...). Safe to call repeatedly."""

    def __enter__(self) -> BaseFetcher:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------- helpers
    @staticmethod
    def is_supported_file(name: str) -> bool:
        return Path(name).suffix.lower() in SUPPORTED_EXTENSIONS

    @staticmethod
    def is_json_file(path: str | Path) -> bool:
        return Path(path).suffix.lower() == JSON_EXTENSION

    def resolve_destination(self, relative: PurePath) -> Path:
        """Map a source-relative path to a destination path, blocking traversal."""
        if relative.is_absolute() or ".." in relative.parts:
            raise FetcherError(f"Unsafe relative path rejected: {relative}")
        target = self.destination_dir / relative
        if not target.resolve().is_relative_to(self.destination_dir.resolve()):
            raise FetcherError(f"Path escapes destination directory: {relative}")
        return target

    @staticmethod
    def already_present(local: Path, expected_size: int) -> bool:
        """True if ``local`` exists with the same byte size (our duplicate test)."""
        try:
            return local.is_file() and local.stat().st_size == expected_size
        except OSError:
            return False

    @staticmethod
    def atomic_replace(tmp: Path, final: Path) -> None:
        """Move a fully written temp file into place so readers never see partial files."""
        os.replace(tmp, final)

    def emit_progress(self, progress: TransferProgress) -> None:
        """Invoke the progress callback; callback errors never abort a transfer."""
        if self._progress_callback is None:
            return
        try:
            self._progress_callback(progress)
        except Exception:  # noqa: BLE001
            logger.exception("Progress callback raised; ignoring")
