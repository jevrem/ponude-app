from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from app import db
from app.security import authenticate, logout

APP_DIR = os.path.dirname(__file__)
TEMPLATES_DIR = os.path.join(APP_DIR, "templates")

app = FastAPI()
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Session cookie (production: set SECRET_KEY in Render env vars)
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=True,
    max_age=60 * 60 * 24 * 30,  # 30 days
)


def _user(request: Request) -> Optional[str]:
    return request.session.get("user")


def _require_login(request: Request) -> Optional[RedirectResponse]:
    if not _user(request):
        return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)
    return None


def _to_decimal(x: str, default: Decimal = Decimal("0")) -> Decimal:
    try:
        x = (x or "").replace(",", ".").strip()
        if x == "":
            return default
        return Decimal(x)
    except (InvalidOperation, ValueError):
        return default


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/", include_in_schema=False)
def root(request: Request):
    if _user(request):
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)


@app.get("/__routes", include_in_schema=False)
def routes_dump():
    # Simple debug helper: list all routes
    routes = []
    for r in app.router.routes:
        methods = getattr(r, "methods", None)
        routes.append({"path": getattr(r, "path", str(r)), "methods": sorted(list(methods)) if methods else None})
    return JSONResponse(routes)


# ---------------------------
# AUTH
# ---------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    # Minimal built-in template to avoid missing file issues
    html = f"""
    <html><head><meta charset="utf-8"><title>Login</title></head>
    <body style="font-family:Arial;max-width:420px;margin:40px auto;">
      <h2>Ponude – prijava</h2>
      {'<div style="color:#b00;font-weight:bold">Pogrešan user ili lozinka.</div>' if error else ''}
      <form method="post" action="/login">
        <label>Username</label><br>
        <input name="username" style="width:100%;padding:10px" /><br><br>
        <label>Lozinka</label><br>
        <input type="password" name="password" style="width:100%;padding:10px" /><br><br>
        <button type="submit" style="width:100%;padding:12px;font-weight:bold">Prijavi se</button>
      </form>
      <div style="margin-top:10px;color:#666;font-size:12px">
        Postavi USER1/2 vars u Render Environment Variables.
      </div>
    </body></html>
    """
    return HTMLResponse(html)


@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if authenticate(username, password):
        request.session["user"] = username.strip().lower()
        # create or fetch active offer
        offer_id = db.ensure_active_offer(request.session["user"])
        request.session["offer_id"] = offer_id
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/login?error=1", status_code=HTTP_303_SEE_OTHER)


@app.post("/logout")
def logout_post(request: Request):
    return logout(request)


# ---------------------------
# OFFER
# ---------------------------

@app.get("/offer", response_class=HTMLResponse)
def offer_page(request: Request):
    redir = _require_login(request)
    if redir:
        return redir

    user = _user(request)
    offer_id = request.session.get("offer_id") or db.ensure_active_offer(user)
    request.session["offer_id"] = offer_id

    offer = db.get_offer(offer_id)
    items = db.list_items(offer_id)

    totals = db.compute_totals(items, vat_rate=Decimal(str(offer.get("vat_rate") or 0)))
    ctx = {
        "request": request,
        "user": user,
        "offer": offer,
        "items": items,
        "totals": totals,
    }
    return templates.TemplateResponse("offer.html", ctx)


@app.post("/offer/new")
def offer_new(request: Request):
    redir = _require_login(request)
    if redir:
        return redir
    user = _user(request)
    offer_id = db.create_offer(user)
    request.session["offer_id"] = offer_id
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/item/add")
def offer_item_add(
    request: Request,
    name: str = Form(""),
    qty: str = Form("1"),
    price: str = Form("0"),
):
    redir = _require_login(request)
    if redir:
        return redir
    user = _user(request)
    offer_id = request.session.get("offer_id") or db.ensure_active_offer(user)
    request.session["offer_id"] = offer_id

    offer = db.get_offer(offer_id)
    # Only lock on ACCEPTED (SENT can still be edited)
    if (offer.get("status") or "DRAFT").upper() == "ACCEPTED":
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

    db.add_item(
        offer_id=offer_id,
        name=(name or "").strip(),
        qty=_to_decimal(qty, Decimal("1")),
        price=_to_decimal(price, Decimal("0")),
    )
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/item/delete")
def offer_item_delete(request: Request, item_id: int = Form(...)):
    redir = _require_login(request)
    if redir:
        return redir
    offer_id = request.session.get("offer_id")
    if offer_id:
        offer = db.get_offer(offer_id)
        if (offer.get("status") or "DRAFT").upper() != "ACCEPTED":
            db.delete_item(offer_id, int(item_id))
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/items/clear")
def offer_items_clear(request: Request):
    redir = _require_login(request)
    if redir:
        return redir
    offer_id = request.session.get("offer_id")
    if offer_id:
        offer = db.get_offer(offer_id)
        if (offer.get("status") or "DRAFT").upper() != "ACCEPTED":
            db.clear_items(offer_id)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/client")
def offer_client_save(request: Request, client_name: str = Form("")):
    redir = _require_login(request)
    if redir:
        return redir
    offer_id = request.session.get("offer_id")
    if offer_id:
        offer = db.get_offer(offer_id)
        if (offer.get("status") or "DRAFT").upper() != "ACCEPTED":
            db.set_client_name(offer_id, client_name.strip() or None)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/status")
def offer_status_save(request: Request, status: str = Form("DRAFT")):
    redir = _require_login(request)
    if redir:
        return redir
    offer_id = request.session.get("offer_id")
    if offer_id:
        # Do NOT auto-switch status anywhere else. Only here or accept.
        db.set_status(offer_id, (status or "DRAFT").strip().upper())
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/accept")
def offer_accept(request: Request):
    redir = _require_login(request)
    if redir:
        return redir
    offer_id = request.session.get("offer_id")
    if offer_id:
        db.set_status(offer_id, "ACCEPTED")
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/meta")
def offer_meta_save(
    request: Request,
    place: str = Form(""),
    signed_by: str = Form(""),
    delivery_term: str = Form(""),
    payment_term: str = Form(""),
    note: str = Form(""),
    vat_rate: str = Form("0"),
):
    redir = _require_login(request)
    if redir:
        return redir
    offer_id = request.session.get("offer_id")
    if offer_id:
        offer = db.get_offer(offer_id)
        if (offer.get("status") or "DRAFT").upper() != "ACCEPTED":
            db.set_meta(
                offer_id=offer_id,
                place=place.strip() or None,
                signed_by=signed_by.strip() or None,
                delivery_term=delivery_term.strip() or None,
                payment_term=payment_term.strip() or None,
                note=note.strip() or None,
                vat_rate=int((_to_decimal(vat_rate, Decimal("0")))),
            )
    # Return JSON so the browser doesn't land on /offer/meta (and show 404)
    return JSONResponse({"ok": True})


# ---------------------------
# LIST OFFERS
# ---------------------------

@app.get("/offers", response_class=HTMLResponse)
def offers_page(request: Request):
    redir = _require_login(request)
    if redir:
        return redir
    user = _user(request)
    offers = db.list_offers(user)
    return templates.TemplateResponse("offers.html", {"request": request, "user": user, "offers": offers})


@app.post("/offers/open")
def offers_open(request: Request, offer_id: int = Form(...)):
    redir = _require_login(request)
    if redir:
        return redir
    request.session["offer_id"] = int(offer_id)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


# ---------------------------
# SETTINGS
# ---------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, ok: int = 0):
    redir = _require_login(request)
    if redir:
        return redir
    user = _user(request)
    settings = db.get_settings(user)
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "user": user, "settings": settings, "ok": bool(ok)},
    )


@app.post("/settings")
def settings_save(
    request: Request,
    company_name: str = Form(""),
    company_address: str = Form(""),
    company_oib: str = Form(""),
    company_iban: str = Form(""),
    company_email: str = Form(""),
    company_phone: str = Form(""),
    logo_path: str = Form(""),
):
    redir = _require_login(request)
    if redir:
        return redir
    user = _user(request)
    db.save_settings(
        user=user,
        company_name=company_name.strip() or None,
        company_address=company_address.strip() or None,
        company_oib=company_oib.strip() or None,
        company_iban=company_iban.strip() or None,
        company_email=company_email.strip() or None,
        company_phone=company_phone.strip() or None,
        logo_path=logo_path.strip() or None,
    )
    return RedirectResponse(url="/settings?ok=1", status_code=HTTP_303_SEE_OTHER)
