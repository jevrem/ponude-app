import os
import hmac
from typing import Optional

from fastapi import Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_303_SEE_OTHER


def _users() -> dict[str, str]:
    """Load users from env.

    Format:
      USERS=marko:lozinka,ana:lozinka2

    If not provided, falls back to two demo users (change in production).
    """
    raw = os.getenv("USERS", "").strip()
    if not raw:
        return {
            "marko": os.getenv("MARKO_PASSWORD", "1234"),
            "ana": os.getenv("ANA_PASSWORD", "1234"),
        }

    users: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            continue
        u, p = part.split(":", 1)
        u = u.strip()
        p = p.strip()
        if u:
            users[u] = p
    return users


def verify_credentials(username: str, password: str) -> bool:
    username = (username or "").strip()
    password = (password or "").strip()
    if not username or not password:
        return False

    users = _users()
    expected = users.get(username)
    if expected is None:
        return False

    # constant-time compare
    return hmac.compare_digest(str(expected), str(password))


def require_login(request: Request) -> str:
    """Ensure session user exists.

    Raises a proper HTTPException with 303 + Location header, so Starlette can redirect.
    """
    user = request.session.get("user")
    if not user:
        raise StarletteHTTPException(
            status_code=HTTP_303_SEE_OTHER,
            detail="Not authenticated",
            headers={"Location": "/login"},
        )
    return user


def logout(request: Request) -> None:
    request.session.clear()
