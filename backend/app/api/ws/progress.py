"""Live ingestion progress over WebSockets.

Moving parts
------------
* :class:`JobRegistry`      - thread-safe in-memory state of every ingestion job. A client that
                              connects late (or reconnects) immediately receives the latest state.
* :class:`ConnectionManager` - the sockets listening to each job. Everything that touches the
                              socket sets runs on the event loop; worker threads hand work over
                              with :meth:`ConnectionManager.publish_threadsafe`.
* :class:`ProgressReporter`  - used by the background job (a worker *thread*) to record progress.
                              ``reporter.on_transfer`` plugs straight into a fetcher's
                              ``progress_callback``.

Limitation: state lives in this process's memory, so run a single API worker, or replace the
registry/manager with Redis pub/sub when scaling out.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.config import get_settings
from app.db.schemas import IngestionStats, TransferStats
from app.services.fetchers.base import TransferProgress

logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "running", "parsing", "completed", "failed"]
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed"})

WS_CLOSE_UNKNOWN_JOB = 4404
THROTTLE_SECONDS = 0.25


# ------------------------------------------------------------------------------- job state
@dataclass
class JobState:
    job_id: str
    status: JobStatus = "queued"
    percentage: int = 0
    processed: int = 0
    total: int = 0
    message: str = ""
    current_file: str | None = None
    result: dict[str, Any] | None = None
    updated_at: float = field(default_factory=time.time)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_payload(self) -> dict[str, Any]:
        """JSON sent to clients; ``job_id/percentage/processed/total`` are the core fields."""
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "percentage": self.percentage,
            "processed": self.processed,
            "total": self.total,
            "status": self.status,
            "message": self.message,
            "current_file": self.current_file,
        }
        if self.result is not None:
            payload["result"] = self.result
        return payload


class JobRegistry:
    """Thread-safe store of job states, bounded in size."""

    MAX_JOBS = 500

    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}
        self._lock = threading.Lock()

    def create(self) -> JobState:
        state = JobState(job_id=uuid.uuid4().hex)
        with self._lock:
            self._jobs[state.job_id] = state
            self._evict_locked()
            return replace(state)

    def get(self, job_id: str) -> JobState | None:
        with self._lock:
            state = self._jobs.get(job_id)
            return replace(state) if state else None

    def update(self, job_id: str, **changes: Any) -> JobState | None:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return None
            for key, value in changes.items():
                setattr(state, key, value)
            state.updated_at = time.time()
            return replace(state)

    def _evict_locked(self) -> None:
        """Drop the oldest *finished* jobs once the registry is over its cap."""
        overflow = len(self._jobs) - self.MAX_JOBS
        if overflow <= 0:
            return
        finished = sorted(
            (s for s in self._jobs.values() if s.is_terminal), key=lambda s: s.updated_at
        )
        for state in finished[:overflow]:
            del self._jobs[state.job_id]


# ------------------------------------------------------------------------ connection manager
class ConnectionManager:
    """Tracks the WebSockets subscribed to each job and broadcasts payloads to them."""

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    # Loop binding (called from the app lifespan) lets worker threads reach the loop.
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def unbind_loop(self) -> None:
        self._loop = None

    async def connect(self, job_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.setdefault(job_id, set()).add(websocket)

    def disconnect(self, job_id: str, websocket: WebSocket) -> None:
        sockets = self._connections.get(job_id)
        if sockets is None:
            return
        sockets.discard(websocket)
        if not sockets:
            del self._connections[job_id]

    async def broadcast(self, job_id: str, payload: dict[str, Any]) -> None:
        """Send ``payload`` to every listener of ``job_id``; dead sockets are dropped."""
        for websocket in list(self._connections.get(job_id, ())):
            try:
                await websocket.send_json(payload)
            except Exception:  # noqa: BLE001 - a broken client must not affect the others
                self.disconnect(job_id, websocket)

    async def close_job(self, job_id: str, code: int = 1000) -> None:
        """Close and forget every socket listening to ``job_id`` (job finished)."""
        for websocket in list(self._connections.pop(job_id, ())):
            try:
                await websocket.close(code=code)
            except Exception:  # noqa: BLE001
                logger.debug("Error closing websocket for job %s", job_id, exc_info=True)

    async def _publish(self, job_id: str, payload: dict[str, Any], close: bool) -> None:
        await self.broadcast(job_id, payload)
        if close:
            await self.close_job(job_id)

    def publish_threadsafe(
        self, job_id: str, payload: dict[str, Any], *, close: bool = False
    ) -> None:
        """Broadcast from any thread (e.g. the background download thread)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._publish(job_id, payload, close), loop)


job_registry = JobRegistry()
manager = ConnectionManager()


# ---------------------------------------------------------------------------- the reporter
class ProgressReporter:
    """Records and publishes progress for one job. Safe to call from worker threads."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self._last_sent = 0.0
        self._last_files_done = -1
        self._percentage = 0

    def _push(self, *, close: bool = False, **changes: Any) -> None:
        state = job_registry.update(self.job_id, **changes)
        if state is not None:
            manager.publish_threadsafe(self.job_id, state.to_payload(), close=close)

    def running(self, message: str = "Connecting to remote host") -> None:
        self._push(status="running", message=message)

    def on_transfer(self, progress: TransferProgress) -> None:
        """Fetcher ``progress_callback``; per-chunk calls are throttled to ~4 updates/second."""
        now = time.monotonic()
        file_finished = progress.files_done != self._last_files_done
        if not file_finished and now - self._last_sent < THROTTLE_SECONDS:
            return
        self._last_sent = now
        self._last_files_done = progress.files_done

        total = max(progress.files_total, 1)
        in_flight = 0.0
        if 0 < progress.bytes_done < progress.bytes_total:
            in_flight = progress.bytes_done / progress.bytes_total  # partial current file
        pct = int(min(100.0, (progress.files_done + in_flight) / total * 100))
        self._percentage = max(self._percentage, pct)  # never move backwards

        self._push(
            status="running",
            percentage=self._percentage,
            processed=progress.files_done,
            total=progress.files_total,
            current_file=progress.filename,
            message="Downloading packages",
        )

    def parsing(self) -> None:
        self._push(
            status="parsing", percentage=100, current_file=None, message="Parsing packages"
        )

    def complete(self, transfer: TransferStats, parse: IngestionStats) -> None:
        self._push(
            status="completed",
            percentage=100,
            current_file=None,
            message="Ingestion complete",
            result={"transfer": transfer.model_dump(), "parse": parse.model_dump()},
            close=True,
        )

    def fail(self, message: str) -> None:
        self._push(status="failed", current_file=None, message=message, close=True)


# ------------------------------------------------------------------------------ the route
router = APIRouter(tags=["websockets"])


def _origin_allowed(websocket: WebSocket) -> bool:
    """CORS does not cover WebSockets, so check the browser's Origin header ourselves."""
    origin = websocket.headers.get("origin")
    if origin is None:  # non-browser clients send no Origin
        return True
    if origin in get_settings().cors_origins:
        return True
    # Same-origin pages (e.g. served through the nginx proxy) always match the Host header.
    host = websocket.headers.get("host")
    return bool(host) and urlparse(origin).netloc == host


@router.websocket("/ingest-progress/{job_id}")
async def ingest_progress(websocket: WebSocket, job_id: str) -> None:
    """Stream ``{"job_id", "percentage", "processed", "total", "status", ...}`` for a job.

    The current state is sent immediately on connect. The server closes the socket when the
    job completes or fails; clients may send ``"ping"`` to receive ``"pong"``.
    """
    if not _origin_allowed(websocket):
        await websocket.close(code=1008)
        return

    await manager.connect(job_id, websocket)
    try:
        state = job_registry.get(job_id)
        if state is None:
            await websocket.close(code=WS_CLOSE_UNKNOWN_JOB, reason="Unknown job")
            return

        await websocket.send_json(state.to_payload())
        if state.is_terminal:
            await websocket.close(code=1000)
            return

        while True:  # keep the connection open; updates are pushed by the reporter
            message = await websocket.receive_text()
            if message.strip().lower() == "ping":
                await websocket.send_text("pong")
    except (WebSocketDisconnect, RuntimeError):
        pass  # client left, or socket already closed by the server
    finally:
        manager.disconnect(job_id, websocket)
