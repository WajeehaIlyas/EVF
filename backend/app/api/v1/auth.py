"""Sign-in: username + password only.

    POST /analytics/auth/login    {"username", "password"} -> sets the HttpOnly session cookie
    POST /analytics/auth/logout   clears the cookie
    GET  /analytics/auth/me       the signed-in user
    GET  /analytics/auth/verify   204 when the cookie is valid, 401 otherwise (nginx asks this
                                  before serving /api/, /media/ and /ws/, see nginx/nginx.conf)

The session is an HttpOnly, SameSite=Lax cookie (``evf_session``), so the browser sends it with
every request, including <img> tags and download links, and page scripts cannot read it. An
``Authorization: Bearer <token>`` header is accepted too, for scripts.

(Mounted under /analytics together with the other routers, so no change to main.py is needed.)
Create users with:  python -m app.scripts.create_user <username>

``get_current_user`` is a FastAPI dependency; add it to any router that must need a sign-in:
``app.include_router(x.router, dependencies=[Depends(get_current_user)])``.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import User
from app.services.auth_core import (
    TOKEN_TTL_SECONDS, hash_password, make_token, read_token, verify_password,
)

COOKIE = "evf_session"
PUBLIC_ROUTES = ("/auth/login", "/auth/logout")  # the only routes that work while signed out

router = APIRouter(prefix="/auth", tags=["auth"])

_FAILS: dict[str, deque[float]] = defaultdict(deque)
_MAX_FAILS, _WINDOW = 8, 300  # 8 wrong passwords per username per 5 minutes
_DUMMY_HASH = hash_password("not-a-real-password")  # makes unknown users cost the same time


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    expires_at: int


def _too_many(username: str) -> bool:
    attempts = _FAILS[username]
    while attempts and attempts[0] < time.time() - _WINDOW:
        attempts.popleft()
    return len(attempts) >= _MAX_FAILS


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> LoginOut:
    username = body.username.strip().lower()
    if _too_many(username):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Wait a few minutes and try again.")
    user = db.scalar(select(User).where(User.username == username))
    ok = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
    if not (user and user.is_active and ok):
        _FAILS[username].append(time.time())
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password.")
    _FAILS.pop(username, None)
    token, expires = make_token(user.username)
    response.set_cookie(
        COOKIE, token, max_age=TOKEN_TTL_SECONDS, httponly=True, samesite="lax", path="/",
        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
    )
    return LoginOut(access_token=token, username=user.username, expires_at=expires)


def _token_from(request: Request) -> str | None:
    scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and bearer:
        return bearer
    return request.cookies.get(COOKIE)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = _token_from(request)
    username = read_token(token) if token else None
    user = db.scalar(select(User).where(User.username == username)) if username else None
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required.", headers={"WWW-Authenticate": "Bearer"})
    return user


def require_login(request: Request) -> None:
    """Router-level guard: every route needs a valid session except sign-in and sign-out.

    Only the signature and expiry are checked here (no database trip), so it stays cheap; routes
    that need the user row use ``get_current_user``.
    """
    route = request.scope.get("route")
    if route is not None and str(getattr(route, "path", "")).endswith(PUBLIC_ROUTES):
        return
    token = _token_from(request)
    if not token or read_token(token) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required.", headers={"WWW-Authenticate": "Bearer"})


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


@router.get("/verify", status_code=status.HTTP_204_NO_CONTENT)
def verify() -> None:
    """Reached only with a valid session (``require_login`` runs first); nginx uses the status."""


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict[str, str]:
    return {"username": user.username}


def create_or_update_user(db: Session, username: str, password: str) -> User:
    username = username.strip().lower()
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        user = User(username=username, password_hash=hash_password(password), is_active=True)
        db.add(user)
    else:
        user.password_hash = hash_password(password)
        user.is_active = True
    db.commit()
    return user
