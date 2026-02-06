from __future__ import annotations

import os
import json
import tempfile
from decimal import Decimal, InvalidOperation
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .security import verify_credentials, require_login
from . import db

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)

app = FastAPI()

# sessions (cookie-based)
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)

# static
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def _startup() -> None:
    db.init()


def render(request: Request, name: str, context: dict) -> HTMLResponse:
    tpl = env.get_template(name)
    html = tpl.render(**context)
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    # redirect based on session
    if request.session.get("user"):
        return RedirectResponse("/offer", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    return render(request, "login.html", {"error": None})


@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_credentials(username, password):
        request.session["user"] = username.strip()
        return RedirectResponse("/offer", status_code=HTTP_303_SEE_OTHER)
    return render(request, "login.html", {"error": "Pogrešno korisničko ime ili lozinka."})


@app.post("/logout")
def logout_post(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)


def _dec(s: Any) -> Decimal:
    try:
        return Decimal(str(s or "0").replace(",", "."))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _active_offer_id(request: Request) -> int:
    oid = request.session.get("active_offer_id")
    if isinstance(oid, int) and oid > 0:
        return oid
    # fallback: create new offer
    oid = db.create_offer(user=require_login(request))
    request.session["active_offer_id"] = oid
    return oid


@app.post("/offer/new")
def offer_new(request: Request):
    user = require_login(request)
    oid = db.create_offer(user=user)
    request.session["active_offer_id"] = oid
    return RedirectResponse("/offer", status_code=HTTP_303_SEE_OTHER)


@app.get("/offer", response_class=HTMLResponse)
def offer_get(request: Request):
    user = require_login(request)
    oid = _active_offer_id(request)
    offer = db.get_offer(oid)
    if offer is None:
        oid = db.create_offer(user=user)
        request.session["active_offer_id"] = oid
        offer = db.get_offer(oid)

    items = db.list_items(oid)
    settings = db.get_settings(user) or {}

    # compute totals
    subtotal = sum((_dec(it["qty"]) * _dec(it["price"])) for it in items)
    vat_rate = _dec(offer.get("vat_rate", 0))
    vat = (subtotal * vat_rate / Decimal("100")) if vat_rate else Decimal("0")
    total = subtotal + vat

    locked = str(offer.get("status") or "DRAFT").upper() == "ACCEPTED"

    return render(request, "offer.html", {
        "user": user,
        "offer": offer,
        "items": items,
        "subtotal": f"{subtotal:.2f}",
        "vat_rate": f"{vat_rate:.0f}",
        "vat": f"{vat:.2f}",
        "total": f"{total:.2f}",
        "locked": locked,
        "settings": settings,
    })


@app.post("/offer/client")
def offer_save_client(request: Request, client_name: str = Form("")):
    user = require_login(request)
    oid = _active_offer_id(request)
    db.update_offer_fields(oid, {"client_name": client_name})
    return RedirectResponse("/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/status")
def offer_save_status(request: Request, status: str = Form("DRAFT")):
    require_login(request)
    oid = _active_offer_id(request)
    # only lock on ACCEPTED
    status_up = (status or "DRAFT").upper()
    if status_up not in ("DRAFT", "SENT", "ACCEPTED"):
        status_up = "DRAFT"
    db.update_offer_fields(oid, {"status": status_up})
    return RedirectResponse("/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/meta")
async def offer_save_meta(request: Request):
    """Save PDF meta (place, delivery, payment, note, signature, vat) via JSON or form-data."""
    user = require_login(request)
    oid = _active_offer_id(request)

    payload: Dict[str, Any] = {}
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        payload = await request.json()
    else:
        form = await request.form()
        payload = dict(form)

    fields = {
        "place": payload.get("place", ""),
        "delivery": payload.get("delivery", ""),
        "payment": payload.get("payment", ""),
        "note": payload.get("note", ""),
        "signature": payload.get("signature", ""),
    }
    vat_rate = payload.get("vat_rate", payload.get("vat", payload.get("pdv", "")))
    try:
        vat_rate = int(str(vat_rate).replace("%", "").strip() or "0")
    except ValueError:
        vat_rate = 0
    if vat_rate not in (0, 5, 13, 25):
        vat_rate = 0
    fields["vat_rate"] = vat_rate

    db.update_offer_fields(oid, fields)
    return JSONResponse({"ok": True})


@app.post("/item/add")
def item_add(request: Request, name: str = Form(...), qty: str = Form("1"), price: str = Form("0")):
    require_login(request)
    oid = _active_offer_id(request)
    offer = db.get_offer(oid) or {}
    if str(offer.get("status") or "DRAFT").upper() == "ACCEPTED":
        return RedirectResponse("/offer", status_code=HTTP_303_SEE_OTHER)

    q = float(str(qty).replace(",", ".") or "1")
    p = float(str(price).replace(",", ".") or "0")
    db.add_item(oid, name=name, qty=q, price=p)
    return RedirectResponse("/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/item/delete")
def item_delete(request: Request, item_id: int = Form(...)):
    require_login(request)
    oid = _active_offer_id(request)
    offer = db.get_offer(oid) or {}
    if str(offer.get("status") or "DRAFT").upper() == "ACCEPTED":
        return RedirectResponse("/offer", status_code=HTTP_303_SEE_OTHER)
    db.delete_item(oid, item_id)
    return RedirectResponse("/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/items/clear")
def items_clear(request: Request):
    require_login(request)
    oid = _active_offer_id(request)
    offer = db.get_offer(oid) or {}
    if str(offer.get("status") or "DRAFT").upper() == "ACCEPTED":
        return RedirectResponse("/offer", status_code=HTTP_303_SEE_OTHER)
    db.clear_items(oid)
    return RedirectResponse("/offer", status_code=HTTP_303_SEE_OTHER)


@app.get("/offers", response_class=HTMLResponse)
def offers_get(request: Request):
    user = require_login(request)
    rows = db.list_offers(user)
    return render(request, "offers.html", {"user": user, "rows": rows})


@app.post("/offers/open")
def offers_open(request: Request, offer_id: int = Form(...)):
    require_login(request)
    request.session["active_offer_id"] = offer_id
    return RedirectResponse("/offer", status_code=HTTP_303_SEE_OTHER)


@app.get("/settings", response_class=HTMLResponse)
def settings_get(request: Request):
    user = require_login(request)
    s = db.get_settings(user) or {}
    return render(request, "settings.html", {"user": user, "settings": s, "ok": False})


@app.post("/settings")
def settings_post(
    request: Request,
    company_name: str = Form(""),
    company_address: str = Form(""),
    company_oib: str = Form(""),
    company_iban: str = Form(""),
    company_email: str = Form(""),
    company_phone: str = Form(""),
    logo_path: str = Form(""),
):
    user = require_login(request)
    db.save_settings(user, {
        "company_name": company_name,
        "company_address": company_address,
        "company_oib": company_oib,
        "company_iban": company_iban,
        "company_email": company_email,
        "company_phone": company_phone,
        "logo_path": logo_path,
    })
    s = db.get_settings(user) or {}
    return render(request, "settings.html", {"user": user, "settings": s, "ok": True})


@app.get("/download/pdf")
def download_pdf(request: Request):
    user = require_login(request)
    oid = _active_offer_id(request)
    offer = db.get_offer(oid) or {}
    items = db.list_items(oid)
    settings = db.get_settings(user) or {}

    pdf_bytes = db.render_offer_pdf(offer, items, settings, static_dir=str(STATIC_DIR))
    filename = f"ponuda_{user}_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{filename}"'
    })


@app.get("/download/excel")
def download_excel(request: Request):
    user = require_login(request)
    oid = _active_offer_id(request)
    offer = db.get_offer(oid) or {}
    items = db.list_items(oid)
    xls_bytes = db.render_offer_excel(offer, items)
    filename = f"ponuda_{user}_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.xlsx"
    return Response(content=xls_bytes, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={
        "Content-Disposition": f'attachment; filename="{filename}"'
    })


@app.get("/__routes")
def debug_routes():
    return {"routes": sorted([f"{','.join(r.methods or [])} {r.path}" for r in app.router.routes])}
