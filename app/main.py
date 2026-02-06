from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from . import db
from .security import require_login, verify_credentials, logout


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI()

# MUST be set in Render env vars for production
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


def _get_offer_id(request: Request) -> int | None:
    oid = request.session.get("offer_id")
    try:
        return int(oid) if oid is not None else None
    except Exception:
        return None


def _ensure_offer(request: Request, user: str) -> int:
    oid = _get_offer_id(request)
    if oid is not None:
        off = db.get_offer(user, oid)
        if off:
            return oid
    # create new
    new_id = db.create_offer(user, None)
    request.session["offer_id"] = int(new_id)
    return int(new_id)


def _money(x: Any) -> Decimal:
    try:
        return Decimal(str(x or 0))
    except Exception:
        return Decimal("0")


def _calc(items: list[dict]) -> tuple[float, float, float]:
    subtotal = sum(float(it.get("line_total") or 0) for it in items)
    vat_rate = 0
    return float(subtotal), float(vat_rate), 0.0


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    # redirect to offer; offer route handles login
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "err": None})


@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_credentials(username, password):
        request.session["user"] = username.strip().lower()
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("login.html", {"request": request, "err": "Pogrešan korisnik ili lozinka."})


@app.post("/logout")
def logout_post(request: Request):
    logout(request)
    return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)


@app.get("/offer", response_class=HTMLResponse)
def offer_page(request: Request):
    user = require_login(request)
    offer_id = _ensure_offer(request, user)
    offer = dict(db.get_offer(user, offer_id) or {})
    if not offer.get("vat_rate"):
        offer["vat_rate"] = 25
    items = db.list_items(offer_id)
    settings = db.get_settings(user)

    subtotal = sum(float(it.get("line_total") or 0) for it in items)
    vat_rate = 25.0
    vat = subtotal * (vat_rate / 100.0) if vat_rate else 0.0
    total = subtotal + vat

    return templates.TemplateResponse(
        "offer.html",
        {
            "request": request,
            "user": user,
            "offer": offer,
            "items": items,
            "settings": settings,
            "subtotal": subtotal,
            "vat": vat,
            "total": total,
            "ok": request.query_params.get("ok"),
            "err": request.query_params.get("err"),
        },
    )


@app.post("/offer/new")
def offer_new(request: Request):
    user = require_login(request)
    oid = db.create_offer(user, None)
    request.session["offer_id"] = int(oid)
    return RedirectResponse(url="/offer?ok=Nova+ponuda+kreirana", status_code=HTTP_303_SEE_OTHER)


@app.post("/offers/open")
def offers_open(request: Request, offer_id: int = Form(...)):
    user = require_login(request)
    off = db.get_offer(user, int(offer_id))
    if not off:
        return RedirectResponse(url="/offers?err=Ponuda+ne+postoji", status_code=HTTP_303_SEE_OTHER)
    request.session["offer_id"] = int(offer_id)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/client")
def offer_client(request: Request, client_name: str = Form("")):
    user = require_login(request)
    offer_id = _ensure_offer(request, user)
    db.update_offer_client_name(user, offer_id, (client_name or "").strip() or None)
    return RedirectResponse(url="/offer?ok=Spremljen+klijent", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/items/add")
def item_add(request: Request, name: str = Form(...), qty: float = Form(1), price: float = Form(0)):
    user = require_login(request)
    offer_id = _ensure_offer(request, user)
    offer = dict(db.get_offer(user, offer_id) or {})
    if offer.get("status") == "ACCEPTED":
        return RedirectResponse(url="/offer?err=Ponuda+je+zakljucana", status_code=HTTP_303_SEE_OTHER)

    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(url="/offer?err=Naziv+stavke+je+obavezan", status_code=HTTP_303_SEE_OTHER)

    db.add_item(offer_id, nm, float(qty or 0), float(price or 0))
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/items/delete")
def item_delete(request: Request, item_id: int = Form(...)):
    user = require_login(request)
    offer_id = _ensure_offer(request, user)
    offer = dict(db.get_offer(user, offer_id) or {})
    if offer.get("status") == "ACCEPTED":
        return RedirectResponse(url="/offer?err=Ponuda+je+zakljucana", status_code=HTTP_303_SEE_OTHER)

    db.delete_item(offer_id, int(item_id))
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/items/clear")
def items_clear(request: Request):
    user = require_login(request)
    offer_id = _ensure_offer(request, user)
    offer = dict(db.get_offer(user, offer_id) or {})
    if offer.get("status") == "ACCEPTED":
        return RedirectResponse(url="/offer?err=Ponuda+je+zakljucana", status_code=HTTP_303_SEE_OTHER)

    db.clear_items(offer_id)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/meta")
def offer_meta(
    request: Request,
    terms_delivery: str = Form(""),
    terms_payment: str = Form(""),
    note: str = Form(""),
    place: str = Form(""),
    signed_by: str = Form(""),
    vat_rate: float = Form(25),
):
    user = require_login(request)
    offer_id = _ensure_offer(request, user)
    # Do NOT change status here
    db.update_offer_meta(
        user=user,
        offer_id=offer_id,
        terms_delivery=(terms_delivery or "").strip() or None,
        terms_payment=(terms_payment or "").strip() or None,
        note=(note or "").strip() or None,
        place=(place or "").strip() or None,
        signed_by=(signed_by or "").strip() or None,
        vat_rate=25.0,
    )
    return RedirectResponse(url="/offer?ok=Spremljeni+detalji", status_code=HTTP_303_SEE_OTHER)


@app.get("/offer/pdf")
def offer_pdf(request: Request):
    user = require_login(request)
    offer_id = _ensure_offer(request, user)

    offer = dict(db.get_offer(user, offer_id) or {})
    if not offer.get("vat_rate"):
        offer["vat_rate"] = 25
    items = db.list_items(offer_id)
    settings = db.get_settings(user)

    pdf_bytes = db.render_offer_pdf(offer=offer, items=items, settings=settings, static_dir=str(STATIC_DIR))
    fname = f"ponuda_{user}_{offer.get('offer_no') or offer_id}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/offer/excel")
def offer_excel(request: Request):
    user = require_login(request)
    offer_id = _ensure_offer(request, user)

    offer = dict(db.get_offer(user, offer_id) or {})
    if not offer.get("vat_rate"):
        offer["vat_rate"] = 25
    items = db.list_items(offer_id)

    xls_bytes = db.render_offer_excel(offer=offer, items=items)
    fname = f"ponuda_{user}_{offer.get('offer_no') or offer_id}.xlsx"

    return Response(
        content=xls_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/offers", response_class=HTMLResponse)
def offers_page(request: Request):
    user = require_login(request)
    offers = db.list_offers(user, status=None)
    return templates.TemplateResponse(
        "offers.html",
        {"request": request, "user": user, "offers": offers, "err": request.query_params.get("err")},
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    user = require_login(request)
    settings = db.get_settings(user)
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "user": user, "settings": settings, "ok": request.query_params.get("ok")},
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
    user = require_login(request)
    db.upsert_settings(
        user=user,
        company_name=(company_name or "").strip() or None,
        company_address=(company_address or "").strip() or None,
        company_oib=(company_oib or "").strip() or None,
        company_iban=(company_iban or "").strip() or None,
        company_email=(company_email or "").strip() or None,
        company_phone=(company_phone or "").strip() or None,
        logo_path=(logo_path or "").strip() or None,
    )
    return RedirectResponse(url="/settings?ok=1", status_code=HTTP_303_SEE_OTHER)


@app.get("/__routes")
def debug_routes():
    return {"routes": [getattr(r, "path", str(r)) for r in app.router.routes]}
