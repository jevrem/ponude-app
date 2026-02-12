from __future__ import annotations

import hmac
import os
from fastapi import Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_303_SEE_OTHER, HTTP_403_FORBIDDEN


def admin_credentials() -> tuple[str, str]:
    """Admin-only login.
    Priority:
      1) ADMIN_USERNAME / ADMIN_PASSWORD
      2) USERS="admin:pass,other:pass"  -> first entry is admin
      3) fallback marko/1234
    """
    u = (os.getenv("ADMIN_USERNAME") or "").strip().lower()
    p = (os.getenv("ADMIN_PASSWORD") or "").strip()
    if u and p:
        return u, p

    raw = (os.getenv("USERS") or "").strip()
    if raw and ":" in raw:
        first = raw.split(",")[0].strip()
        if ":" in first:
            u2, p2 = first.split(":", 1)
            u2 = (u2 or "").strip().lower()
            p2 = (p2 or "").strip()
            if u2 and p2:
                return u2, p2

    return "marko", "1234"


def verify_credentials(username: str, password: str) -> bool:
    username = (username or "").strip().lower()
    password = (password or "").strip()
    if not username or not password:
        return False
    admin_u, admin_p = admin_credentials()
    if username != admin_u:
        return False
    return hmac.compare_digest(str(password), str(admin_p))


def is_admin(username: str) -> bool:
    admin_u, _ = admin_credentials()
    return (username or "").strip().lower() == admin_u


def require_login(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise StarletteHTTPException(
            status_code=HTTP_303_SEE_OTHER,
            detail="Not authenticated",
            headers={"Location": "/login"},
        )
    return str(user)


def require_admin(request: Request) -> str:
    user = require_login(request).strip().lower()
    if not is_admin(user):
        raise StarletteHTTPException(status_code=HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def logout(request: Request) -> None:
    request.session.clear()
