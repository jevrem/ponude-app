from __future__ import annotations

import os
from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER


def _load_users() -> dict[str, str]:
    # Two users via env vars
    u1 = (os.environ.get("USER1_USERNAME") or "").strip().lower()
    p1 = os.environ.get("USER1_PASSWORD") or ""
    u2 = (os.environ.get("USER2_USERNAME") or "").strip().lower()
    p2 = os.environ.get("USER2_PASSWORD") or ""
    users = {}
    if u1:
        users[u1] = p1
    if u2:
        users[u2] = p2
    return users


def authenticate(username: str, password: str) -> bool:
    username = (username or "").strip().lower()
    password = password or ""
    users = _load_users()
    if not username or username not in users:
        return False
    return users[username] == password


def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)
