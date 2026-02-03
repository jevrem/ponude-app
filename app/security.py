import os
from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER

def _users_from_env():
    # For MVP: store credentials as Render env vars (not in code).
    u1 = os.getenv("USER1_USERNAME", "")
    p1 = os.getenv("USER1_PASSWORD", "")
    u2 = os.getenv("USER2_USERNAME", "")
    p2 = os.getenv("USER2_PASSWORD", "")
    users = {}
    if u1 and p1:
        users[u1] = p1
    if u2 and p2:
        users[u2] = p2
    return users

def verify_credentials(username: str, password: str) -> bool:
    users = _users_from_env()
    if not users:
        return False
    return users.get(username) == password

def require_login(request: Request):
    if not request.session.get("user"):
        raise RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)

def logout(request: Request):
    request.session.clear()
