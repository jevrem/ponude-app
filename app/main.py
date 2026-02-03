# app/main.py
import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from .security import verify_credentials, require_login, logout
from .db import init_db, list_items, add_item, clear_items

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret-change-me")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=False,  # kasnije prebacimo na True kad sve ide preko https domene
)


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = request.session.get("user")
    if not user:
        return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("app.html", {"request": request, "user": user})


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if request.session.get("user"):
        return RedirectResponse(url="/", status_code=HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_credentials(username, password):
        request.session["user"] = username
        return RedirectResponse(url="/", status_code=HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Pogrešan user ili lozinka."}
    )


@app.post("/logout")
def do_logout(request: Request):
    logout(request)
    return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)


@app.get("/offer", response_class=HTMLResponse)
def offer_page(request: Request):
    require_login(request)
    user = request.session.get("user")

    items = list_items(user)
    # izračun ukupno po stavci + međuzbroj
    for it in items:
        it["total"] = float(it["qty"]) * float(it["price"])
    subtotal = sum(it["total"] for it in items)

    return templates.TemplateResponse(
        "offer.html",
        {
            "request": request,
            "user": user,
            "items": items,
            "subtotal": subtotal,
        },
    )


@app.post("/offer/add")
def offer_add(
    request: Request,
    name: str = Form(...),
    qty: float = Form(...),
    price: float = Form(...),
):
    require_login(request)
    user = request.session.get("user")

    name = (name or "").strip()
    if name:
        add_item(user, name, float(qty), float(price))

    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/clear")
def offer_clear(request: Request):
    require_login(request)
    user = request.session.get("user")
    clear_items(user)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
