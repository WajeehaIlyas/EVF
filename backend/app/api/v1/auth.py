"""Sign-in: username + password only.

    POST /analytics/auth/login   {"username", "password"} -> {"access_token", "username", "expires_at"}
    GET  /analytics/auth/me      the signed-in user (Authorization: Bearer <token>)

(Mounted under /analytics together with the other routers, so no change to main.py is needed.)
Create users with:  python -m app.scripts.create_user <username>

``get_current_user`` is a FastAPI dependency; add it to any router that must need a sign-in:
``app.include_router(x.router, dependencies=[Depends(get_current_user)])``.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import User
from app.services.auth_core import hash_password, make_token, read_token, verify_password

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
def login(body: LoginIn, db: Session = Depends(get_db)) -> LoginOut:
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
    return LoginOut(access_token=token, username=user.username, expires_at=expires)


def get_current_user(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> User:
    scheme, _, token = (authorization or "").partition(" ")
    username = read_token(token) if scheme.lower() == "bearer" else None
    user = db.scalar(select(User).where(User.username == username)) if username else None
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required.", headers={"WWW-Authenticate": "Bearer"})
    return user


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
