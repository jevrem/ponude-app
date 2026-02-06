from __future__ import annotations

import hmac
import os
from typing import Dict

from fastapi import Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_303_SEE_OTHER


def _add(users: Dict[str, str], u: str | None, p: str | None) -> None:
    u = (u or "").strip().lower()
    p = (p or "").strip()
    if u and p:
        users[u] = p


def _users() -> Dict[str, str]:
    """
    Supported env formats (first match wins):
      USERS="marko:1234,ana:1234"
      USER1_USERNAME / USER1_PASSWORD (and USER2_*, USER3_*)
      USER1 / PASS1 (and USER2/PASS2, USER3/PASS3)
    Fallback:
      marko/1234, ana/1234
    """
    users: Dict[str, str] = {}

    raw = (os.getenv("USERS") or "").strip()
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if not part or ":" not in part:
                continue
            u, p = part.split(":", 1)
            _add(users, u, p)
        if users:
            return users

    _add(users, os.getenv("USER1_USERNAME"), os.getenv("USER1_PASSWORD"))
    _add(users, os.getenv("USER2_USERNAME"), os.getenv("USER2_PASSWORD"))
    _add(users, os.getenv("USER3_USERNAME"), os.getenv("USER3_PASSWORD"))
    if users:
        return users

    _add(users, os.getenv("USER1"), os.getenv("PASS1"))
    _add(users, os.getenv("USER2"), os.getenv("PASS2"))
    _add(users, os.getenv("USER3"), os.getenv("PASS3"))
    if users:
        return users

    return {"marko": "1234", "ana": "1234"}


def verify_credentials(username: str, password: str) -> bool:
    username = (username or "").strip().lower()
    password = (password or "").strip()
    if not username or not password:
        return False
    users = _users()
    expected = users.get(username)
    if expected is None:
        return False
    return hmac.compare_digest(str(expected), str(password))


def require_login(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        # raise an HTTP exception with redirect header (do NOT raise RedirectResponse)
        raise StarletteHTTPException(
            status_code=HTTP_303_SEE_OTHER,
            detail="Not authenticated",
            headers={"Location": "/login"},
        )
    return str(user)


def logout(request: Request) -> None:
    request.session.clear()
