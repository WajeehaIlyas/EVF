"""Passwords and sign-in tokens, with the standard library only (no extra packages).

* Passwords: scrypt with a random salt, stored as ``scrypt$n$r$p$salt$hash`` (base64).
* Tokens: ``base64url(payload).base64url(HMAC-SHA256)``; the payload is ``{"sub": username, "exp": unix}``.
  The signing key comes from the EVF_SECRET_KEY environment variable (the older AEVAF_SECRET_KEY name still works). Set it to a long random
  string in production; the built-in fallback is for local development only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

TOKEN_TTL_SECONDS = int(os.environ.get("EVF_TOKEN_TTL_HOURS") or os.environ.get("AEVAF_TOKEN_TTL_HOURS") or "12") * 3600
_DEV_KEY = "evf-development-key-change-me"
_N, _R, _P = 2**14, 8, 1


def secret_key() -> bytes:
    return (os.environ.get("EVF_SECRET_KEY") or os.environ.get("AEVAF_SECRET_KEY") or _DEV_KEY).encode()


def using_dev_key() -> bool:
    return not (os.environ.get("EVF_SECRET_KEY") or os.environ.get("AEVAF_SECRET_KEY"))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(candidate, _unb64(digest))
    except (ValueError, TypeError):
        return False


def make_token(username: str, now: float | None = None, ttl: int = TOKEN_TTL_SECONDS) -> tuple[str, int]:
    expires = int((now if now is not None else time.time()) + ttl)
    body = _b64(json.dumps({"sub": username, "exp": expires}, separators=(",", ":")).encode())
    signature = _b64(hmac.new(secret_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}", expires


def read_token(token: str, now: float | None = None) -> str | None:
    """The username in a valid, unexpired token, else None."""
    try:
        body, signature = token.split(".")
        expected = _b64(hmac.new(secret_key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_unb64(body))
        if payload["exp"] < (now if now is not None else time.time()):
            return None
        return str(payload["sub"])
    except (ValueError, KeyError, TypeError):
        return None
