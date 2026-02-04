import os
import hmac
from fastapi import Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_303_SEE_OTHER


def _add(users: dict[str, str], u: str | None, p: str | None) -> None:
    u = (u or "").strip()
    p = (p or "").strip()
    if u and p:
        users[u] = p


def _users() -> dict[str, str]:
    """Return users from environment in a backward-compatible way.

    Supported formats (checked in this order):

    1) USERS="marko:pass,ana:pass2"
    2) USER1_USERNAME / USER1_PASSWORD (+ USER2_USERNAME / USER2_PASSWORD ...)
    3) USER1 / PASS1 (+ USER2 / PASS2 ...)
    4) MARKO_PASSWORD / ANA_PASSWORD
    5) Fallback demo:
         marko / 1234
         ana   / 1234
    """
    users: dict[str, str] = {}

    raw = os.getenv("USERS", "").strip()
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if not part or ":" not in part:
                continue
            u, p = part.split(":", 1)
            _add(users, u, p)
        if users:
            return users

    # Common Render-style vars
    _add(users, os.getenv("USER1_USERNAME"), os.getenv("USER1_PASSWORD"))
    _add(users, os.getenv("USER2_USERNAME"), os.getenv("USER2_PASSWORD"))
    _add(users, os.getenv("USER3_USERNAME"), os.getenv("USER3_PASSWORD"))
    if users:
        return users

    # Older naming
    _add(users, os.getenv("USER1"), os.getenv("PASS1"))
    _add(users, os.getenv("USER2"), os.getenv("PASS2"))
    _add(users, os.getenv("USER3"), os.getenv("PASS3"))
    if users:
        return users

    # Simple per-user password vars
    _add(users, "marko", os.getenv("MARKO_PASSWORD"))
    _add(users, "ana", os.getenv("ANA_PASSWORD"))
    if users:
        return users

    # Fallback (so you can always log in on a fresh deploy)
    return {"marko": "1234", "ana": "1234"}


def verify_credentials(username: str, password: str) -> bool:
    username = (username or "").strip()
    password = (password or "").strip()
    if not username or not password:
        return False

    users = _users()
    expected = users.get(username)
    if expected is None:
        return False

    return hmac.compare_digest(str(expected), str(password))


def require_login(request: Request) -> str:
    """Require logged-in session; redirect to /login using 303."""
    user = request.session.get("user")
    if not user:
        raise StarletteHTTPException(
            status_code=HTTP_303_SEE_OTHER,
            detail="Not authenticated",
            headers={"Location": "/login"},
        )
    return str(user)


def logout(request: Request) -> None:
    """Clear session."""
    try:
        request.session.clear()
    except Exception:
        # very defensive; sessions middleware should always provide dict-like session
        request.session["user"] = None
