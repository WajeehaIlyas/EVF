"""AEVAF API entry point.

Run (from backend/):  uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.v1 import events, exports, imports, ingestion, reviews, sites
from app.api.v1 import analytics, classified_exports
from app.api.ws import progress
from app.core.config import get_settings
from app.core.database import get_db, init_db

settings = get_settings()
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("aevaf")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Worker threads need the loop to push WebSocket updates.
    progress.manager.bind_loop(asyncio.get_running_loop())
    if settings.environment == "development":
        try:
            init_db()  # convenience only; use migrations outside development
        except SQLAlchemyError:
            logger.exception("Could not create tables - is the database reachable?")
    yield
    progress.manager.unbind_loop()


app = FastAPI(
    title="AEVAF API",
    version="0.4.0",
    description="Automated Event Validation & Analytics Framework",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,  # defaults to http://localhost:3000
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # lets the UI read CSV file names
)

# ---- static media (full databar + cropped images) ---------------------------------------
try:
    settings.media_root.mkdir(parents=True, exist_ok=True)
except OSError:
    logger.warning("Cannot create media root %s; check MEDIA_ROOT and permissions",
                   settings.media_root)
app.mount(
    settings.media_url_prefix,
    StaticFiles(directory=settings.media_root, check_dir=False),
    name="media",
)

# ---- routes -------------------------------------------------------------------------------
api_v1 = APIRouter(prefix="/api/v1")
for module in (sites, events, reviews, ingestion, exports, imports, analytics, classified_exports):
    api_v1.include_router(module.router)

app.include_router(api_v1)
app.include_router(progress.router, prefix="/ws")

@app.get("/health", tags=["system"], summary="Liveness + database check")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


# ---- error handling -----------------------------------------------------------------------
@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.exception("Database error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Database error"})


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=settings.debug)
