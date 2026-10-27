"""Paramiko-based SSH/SFTP fetcher.

Downloads JSON packages and images from a remote directory into
``<media_root>/events/<site_id>/<feature>/``, preserving the remote sub-folder
layout, and skips files that already exist locally with the same size.
"""

from __future__ import annotations

import logging
import posixpath
import socket
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath

import paramiko

from app.core.config import Settings
from app.services.fetchers.base import (
    BaseFetcher,
    FetcherConfigError,
    FetcherConnectionError,
    FetcherError,
    FetchResult,
    ProgressCallback,
    TransferProgress,
)

logger = logging.getLogger(__name__)


@dataclass
class SftpConfig:
    """Connection and scan settings for one SFTP source."""

    host: str
    username: str
    remote_path: str
    port: int = 22
    password: str | None = field(default=None, repr=False)
    private_key_path: str | None = None
    private_key_passphrase: str | None = field(default=None, repr=False)
    recursive: bool = True
    max_depth: int = 5


@dataclass(frozen=True)
class _RemoteFile:
    path: str
    relative: PurePosixPath
    size: int


class SftpFetcher(BaseFetcher):
    """Fetch event packages from a remote host over SFTP."""

    def __init__(
        self,
        site_id: int,
        feature: str,
        config: SftpConfig,
        *,
        settings: Settings | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        super().__init__(site_id, feature, settings=settings, progress_callback=progress_callback)
        if not config.host or not config.username:
            raise FetcherConfigError("SFTP host and username are required")
        if not (config.password or config.private_key_path):
            raise FetcherConfigError("SFTP requires a password or a private key path")
        self.config = config
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None

    # ---------------------------------------------------------- connection
    def connect(self) -> None:
        """Open the SSH + SFTP session (retrying transient network errors)."""
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        known_hosts = self.settings.sftp_known_hosts_file
        if known_hosts is not None:
            try:
                client.load_host_keys(str(known_hosts))
            except OSError as exc:
                raise FetcherConfigError(f"Cannot read known_hosts file {known_hosts}") from exc
        if self.settings.sftp_allow_unknown_hosts:
            logger.warning("SFTP host-key verification is relaxed (insecure)")
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())

        cfg = self.config
        attempts = self.settings.sftp_connect_retries
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                client.connect(
                    hostname=cfg.host,
                    port=cfg.port,
                    username=cfg.username,
                    password=cfg.password,
                    key_filename=cfg.private_key_path,
                    passphrase=cfg.private_key_passphrase,
                    timeout=self.settings.sftp_connect_timeout,
                    banner_timeout=self.settings.sftp_connect_timeout,
                    auth_timeout=self.settings.sftp_connect_timeout,
                    look_for_keys=False,
                    allow_agent=False,
                )
                self._client = client
                self._sftp = client.open_sftp()
                logger.info("Connected to SFTP host %s:%s", cfg.host, cfg.port)
                return
            except paramiko.AuthenticationException as exc:
                client.close()
                raise FetcherConnectionError(f"SFTP authentication failed for {cfg.host}") from exc
            except paramiko.BadHostKeyException as exc:
                client.close()
                raise FetcherConnectionError(f"Host key mismatch for {cfg.host}") from exc
            except (paramiko.SSHException, OSError, EOFError) as exc:
                # Includes unknown-host rejection; retry is harmless but cheap.
                last_error = exc
                logger.warning("SFTP connect attempt %d/%d failed: %s", attempt, attempts, exc)
                if attempt < attempts:
                    time.sleep(min(2**attempt, 10))
        client.close()
        raise FetcherConnectionError(
            f"Could not connect to {cfg.host}:{cfg.port} after {attempts} attempts: {last_error}"
        )

    def close(self) -> None:
        for resource in (self._sftp, self._client):
            if resource is not None:
                try:
                    resource.close()
                except Exception:  # noqa: BLE001
                    logger.debug("Error while closing SFTP resource", exc_info=True)
        self._sftp = None
        self._client = None

    def _require_sftp(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            raise FetcherError("SFTP session is not open; call connect() first")
        return self._sftp

    # ---------------------------------------------------------------- fetch
    def fetch(self) -> FetchResult:
        result = FetchResult(self.site_id, self.feature, self.destination_dir)
        self.destination_dir.mkdir(parents=True, exist_ok=True)

        self.connect()
        try:
            remote_files = self._list_remote_files()
            total = len(remote_files)
            logger.info("Found %d candidate files on %s", total, self.config.host)
            for index, remote in enumerate(remote_files, start=1):
                self._download_one(remote, result, index, total)
        finally:
            self.close()

        result.json_files.sort()
        logger.info(
            "SFTP fetch finished: %d downloaded, %d skipped, %d failed",
            result.downloaded,
            result.skipped,
            result.failed,
        )
        return result

    def _list_remote_files(self) -> list[_RemoteFile]:
        """Walk the remote tree iteratively, returning supported regular files."""
        sftp = self._require_sftp()
        root = self.config.remote_path.rstrip("/") or "/"
        found: list[_RemoteFile] = []
        stack: list[tuple[str, PurePosixPath, int]] = [(root, PurePosixPath("."), 0)]

        while stack:
            current, relative, depth = stack.pop()
            try:
                entries = sftp.listdir_attr(current)
            except OSError as exc:
                if depth == 0:
                    raise FetcherError(f"Cannot list remote path {current!r}: {exc}") from exc
                logger.warning("Skipping unreadable remote directory %s: %s", current, exc)
                continue

            for entry in sorted(entries, key=lambda e: e.filename):
                name = entry.filename
                # Defensive: a hostile server must not be able to inject path separators.
                if name in {".", ".."} or "/" in name or "\\" in name:
                    continue
                mode = entry.st_mode or 0
                child_path = posixpath.join(current, name)
                child_rel = relative / name
                if stat.S_ISLNK(mode):
                    continue  # never follow symlinks
                if stat.S_ISDIR(mode):
                    if self.config.recursive and depth < self.config.max_depth:
                        stack.append((child_path, child_rel, depth + 1))
                elif stat.S_ISREG(mode) and self.is_supported_file(name):
                    found.append(_RemoteFile(child_path, child_rel, entry.st_size or 0))
        return found

    def _download_one(
        self, remote: _RemoteFile, result: FetchResult, index: int, total: int
    ) -> None:
        """Download one file atomically; record (never raise) per-file failures."""
        tmp: Path | None = None
        try:
            local = self.resolve_destination(PurePath(remote.relative.as_posix()))

            if self.already_present(local, remote.size):
                result.skipped += 1
                if self.is_json_file(local):
                    result.json_files.append(local)
                return

            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_name(local.name + ".part")

            def _on_chunk(done: int, size: int) -> None:
                self.emit_progress(
                    TransferProgress(remote.relative.name, done, size, index - 1, total)
                )

            self._require_sftp().get(remote.path, str(tmp), callback=_on_chunk)

            actual = tmp.stat().st_size
            if actual != remote.size:
                raise FetcherError(f"Size mismatch (expected {remote.size}, got {actual})")
            self.atomic_replace(tmp, local)
            tmp = None

            result.downloaded += 1
            result.bytes_transferred += remote.size
            if self.is_json_file(local):
                result.json_files.append(local)
            self.emit_progress(
                TransferProgress(remote.relative.name, remote.size, remote.size, index, total)
            )
        except (OSError, EOFError, paramiko.SSHException, socket.error, FetcherError) as exc:
            result.failed += 1
            message = f"{remote.path}: {exc}"
            result.errors.append(message)
            logger.warning("Download failed - %s", message)
            if tmp is not None:
                tmp.unlink(missing_ok=True)
