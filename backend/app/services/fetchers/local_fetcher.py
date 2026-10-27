"""Local directory scanner / copier for package folders already on disk."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from app.core.config import Settings
from app.services.fetchers.base import (
    BaseFetcher,
    FetcherConfigError,
    FetcherError,
    FetchResult,
    ProgressCallback,
    TransferProgress,
)

logger = logging.getLogger(__name__)


class LocalFetcher(BaseFetcher):
    """Copy supported files from ``source_dir`` into the managed media folder."""

    def __init__(
        self,
        site_id: int,
        feature: str,
        source_dir: str | Path,
        *,
        recursive: bool = True,
        settings: Settings | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        super().__init__(site_id, feature, settings=settings, progress_callback=progress_callback)
        self.source_dir = Path(source_dir).expanduser().resolve()
        self.recursive = recursive
        if not self.source_dir.is_dir():
            raise FetcherConfigError(f"Source folder does not exist: {self.source_dir}")

    def fetch(self) -> FetchResult:
        result = FetchResult(self.site_id, self.feature, self.destination_dir)
        self.destination_dir.mkdir(parents=True, exist_ok=True)
        dest_root = self.destination_dir.resolve()

        # If the source already lives inside the managed folder there is nothing to copy:
        # index the files where they are (avoids copying a folder onto itself).
        in_place = self.source_dir.is_relative_to(dest_root)

        files = self._scan(skip_under=None if in_place else dest_root)
        total = len(files)
        logger.info("Found %d candidate files in %s", total, self.source_dir)

        for index, source in enumerate(files, start=1):
            self._process_one(source, result, index, total, in_place)

        result.json_files.sort()
        return result

    def _scan(self, skip_under: Path | None) -> list[Path]:
        """List supported files under the source folder (symlinks are not followed)."""
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.source_dir, followlinks=False):
            current = Path(dirpath)
            dirnames.sort()
            if skip_under is not None and current.resolve().is_relative_to(skip_under):
                dirnames.clear()  # don't descend into our own destination
                continue
            for name in sorted(filenames):
                path = current / name
                if path.is_symlink() or not self.is_supported_file(name):
                    continue
                found.append(path)
            if not self.recursive:
                break
        return found

    def _process_one(
        self, source: Path, result: FetchResult, index: int, total: int, in_place: bool
    ) -> None:
        tmp: Path | None = None
        try:
            size = source.stat().st_size
            if in_place:
                target = source
                result.skipped += 1
            else:
                relative = source.relative_to(self.source_dir)
                target = self.resolve_destination(relative)
                if self.already_present(target, size):
                    result.skipped += 1
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_name(target.name + ".part")
                    shutil.copy2(source, tmp)
                    self.atomic_replace(tmp, target)
                    tmp = None
                    result.downloaded += 1
                    result.bytes_transferred += size

            if self.is_json_file(target):
                result.json_files.append(target)
            self.emit_progress(TransferProgress(source.name, size, size, index, total))
        except (OSError, shutil.Error, FetcherError) as exc:
            result.failed += 1
            result.errors.append(f"{source}: {exc}")
            logger.warning("Copy failed for %s: %s", source, exc)
            if tmp is not None:
                tmp.unlink(missing_ok=True)
