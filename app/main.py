from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Tuple

from fastapi import FastAPI, File, Form, Request, UploadFile
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


def _user_ctx(request: Request) -> Tuple[str, int]:
    username = require_login(request).strip().lower()
    user_id = db.ensure_user(username)
    return username, int(user_id)


def _ensure_offer(request: Request, username: str, user_id: int) -> int:
    oid = _get_offer_id(request)
    if oid is not None:
        off = db.get_offer(user_id, username, oid)
        if off and not off.get("archived"):
            return oid
    new_id = db.create_offer(user_id, username, None)
    request.session["offer_id"] = int(new_id)
    return int(new_id)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "err": None})


@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_credentials(username, password):
        request.session["user"] = username.strip().lower()
        # ensure user exists in DB
        db.ensure_user(username.strip().lower())
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("login.html", {"request": request, "err": "Pogrešan korisnik ili lozinka."})


@app.post("/logout")
def logout_post(request: Request):
    logout(request)
    return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)


@app.get("/offer", response_class=HTMLResponse)
def offer_page(request: Request):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)
    offer = dict(db.get_offer(user_id, username, offer_id) or {})
    if not offer.get("vat_rate"):
        offer["vat_rate"] = 25
    items = db.list_items(offer_id)
    settings = db.get_settings(user_id, username)

    subtotal = sum(float(it.get("line_total") or 0) for it in items)
    vat_rate = 25.0
    vat = subtotal * (vat_rate / 100.0) if vat_rate else 0.0
    total = subtotal + vat

    return templates.TemplateResponse(
        "offer.html",
        {
            "request": request,
            "user": username,
            "offer": offer,
            "items": items,
            "settings": settings,
            "subtotal": subtotal,
            "vat": vat,
            "total": total,
            "editable": (not offer.get("archived")) and (offer.get("status") != "ACCEPTED"),
            "ok": request.query_params.get("ok"),
            "err": request.query_params.get("err"),
        },
    )


@app.post("/offer/new")
def offer_new(request: Request):
    username, user_id = _user_ctx(request)
    oid = db.create_offer(user_id, username, None)
    request.session["offer_id"] = int(oid)
    return RedirectResponse(url="/offer?ok=Nova+ponuda+kreirana", status_code=HTTP_303_SEE_OTHER)


@app.post("/offers/open")
def offers_open(request: Request, offer_id: int = Form(...)):
    username, user_id = _user_ctx(request)
    off = db.get_offer(user_id, username, int(offer_id))
    if not off:
        return RedirectResponse(url="/offers?err=Ponuda+ne+postoji", status_code=HTTP_303_SEE_OTHER)
    request.session["offer_id"] = int(offer_id)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/client")
def offer_client(request: Request, client_name: str = Form("")):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)
    try:
        db.update_offer_client_name(user_id, username, offer_id, (client_name or "").strip() or None)
    except Exception as e:
        return RedirectResponse(url=f"/offer?err={str(e).replace(' ', '+')}", status_code=HTTP_303_SEE_OTHER)
    # Keep a lightweight client list
    try:
        db.upsert_client(user_id, username, (client_name or "").strip())
    except Exception:
        pass
    return RedirectResponse(url="/offer?ok=Spremljen+klijent", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/items/add")
def item_add(request: Request, name: str = Form(...), qty: float = Form(1), price: float = Form(0)):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)

    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(url="/offer?err=Naziv+stavke+je+obavezan", status_code=HTTP_303_SEE_OTHER)

    try:
        db.add_item(user_id, username, offer_id, nm, float(qty or 0), float(price or 0))
    except Exception as e:
        return RedirectResponse(url=f"/offer?err={str(e).replace(' ', '+')}", status_code=HTTP_303_SEE_OTHER)

    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/items/delete")
def item_delete(request: Request, item_id: int = Form(...)):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)
    try:
        db.delete_item(user_id, username, offer_id, int(item_id))
    except Exception as e:
        return RedirectResponse(url=f"/offer?err={str(e).replace(' ', '+')}", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/items/clear")
def items_clear(request: Request):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)
    try:
        db.clear_items(user_id, username, offer_id)
    except Exception as e:
        return RedirectResponse(url=f"/offer?err={str(e).replace(' ', '+')}", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/meta")
def offer_meta(
    request: Request,
    client_email: str = Form(""),
    terms_delivery: str = Form(""),
    terms_payment: str = Form(""),
    note: str = Form(""),
    place: str = Form(""),
    signed_by: str = Form(""),
    vat_rate: float = Form(25),
):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)
    try:
        db.update_offer_client_email(user_id, username, offer_id, (client_email or "").strip() or None)
        db.update_offer_meta(
            user_id=user_id,
            username=username,
            offer_id=offer_id,
            terms_delivery=(terms_delivery or "").strip() or None,
            terms_payment=(terms_payment or "").strip() or None,
            note=(note or "").strip() or None,
            place=(place or "").strip() or None,
            signed_by=(signed_by or "").strip() or None,
            vat_rate=25.0,
        )
    except Exception as e:
        return RedirectResponse(url=f"/offer?err={str(e).replace(' ', '+')}", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/offer?ok=Spremljeni+detalji", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/accept")
def offer_accept(request: Request):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)
    db.accept_offer(user_id, username, offer_id)
    return RedirectResponse(url="/offer?ok=Ponuda+je+zaključana+(ACCEPTED)", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/unlock")
def offer_unlock(request: Request):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)
    db.unlock_offer(user_id, username, offer_id)
    return RedirectResponse(url="/offer?ok=Ponuda+je+otključana+(DRAFT)", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/archive")
def offer_archive(request: Request):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)
    db.archive_offer(user_id, username, offer_id)
    # Clear active offer in session to avoid editing archived one
    request.session.pop("offer_id", None)
    return RedirectResponse(url="/offers?ok=1", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/unarchive")
def offer_unarchive(request: Request, offer_id: int = Form(...)):
    username, user_id = _user_ctx(request)
    db.unarchive_offer(user_id, username, int(offer_id))
    return RedirectResponse(url="/offers?ok=1&show=archived", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/delete")
def offer_delete(request: Request, offer_id: int = Form(...)):
    username, user_id = _user_ctx(request)
    db.delete_offer_permanently(user_id, username, int(offer_id))
    return RedirectResponse(url="/offers?ok=1&show=archived", status_code=HTTP_303_SEE_OTHER)


@app.get("/offer/pdf")
def offer_pdf(request: Request):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)

    offer = dict(db.get_offer(user_id, username, offer_id) or {})
    if not offer.get("vat_rate"):
        offer["vat_rate"] = 25
    items = db.list_items(offer_id)
    settings = db.get_settings(user_id, username)
    logo_bytes, _mime = db.get_logo_bytes(user_id, username)

    pdf_bytes = db.render_offer_pdf(offer=offer, items=items, settings=settings, static_dir=str(STATIC_DIR), logo_bytes=logo_bytes)
    fname = f"ponuda_{username}_{offer.get('offer_no') or offer_id}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/offer/excel")
def offer_excel(request: Request):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)

    offer = dict(db.get_offer(user_id, username, offer_id) or {})
    if not offer.get("vat_rate"):
        offer["vat_rate"] = 25
    items = db.list_items(offer_id)

    xls_bytes = db.render_offer_excel(offer=offer, items=items)
    fname = f"ponuda_{username}_{offer.get('offer_no') or offer_id}.xlsx"

    return Response(
        content=xls_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )



@app.post("/offer/send")
def offer_send(request: Request, to_email: str = Form(""), subject: str = Form(""), body: str = Form("")):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)

    offer = dict(db.get_offer(user_id, username, offer_id) or {})
    items = db.list_items(offer_id)
    settings = db.get_settings(user_id, username)
    logo_bytes, _mime = db.get_logo_bytes(user_id, username)

    recipient = (to_email or "").strip() or (offer.get("client_email") or "").strip()
    if not recipient:
        return RedirectResponse(url="/offer?err=Nedostaje+email+klijenta", status_code=HTTP_303_SEE_OTHER)

    smtp_host = (os.getenv("SMTP_HOST") or "").strip()
    smtp_user = (os.getenv("SMTP_USER") or "").strip()
    smtp_pass = (os.getenv("SMTP_PASS") or "").strip()
    smtp_port = int(os.getenv("SMTP_PORT") or "587")
    smtp_tls = (os.getenv("SMTP_TLS") or "1").strip() not in {"0", "false", "False", "no", "NO"}

    if not smtp_host or not smtp_user or not smtp_pass:
        return RedirectResponse(url="/offer?err=SMTP+nije+konfiguriran+(SMTP_HOST/SMTP_USER/SMTP_PASS)", status_code=HTTP_303_SEE_OTHER)

    pdf_bytes = db.render_offer_pdf(offer=offer, items=items, settings=settings, static_dir=str(STATIC_DIR), logo_bytes=logo_bytes)
    offer_no = offer.get("offer_no") or str(offer_id)

    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg["Subject"] = subject.strip() or f"Ponuda {offer_no}"
    text_body = body.strip() or f"Pozdrav,\n\nU privitku je ponuda {offer_no}.\n\nLijep pozdrav,\n{settings.get('company_name') or username}"
    msg.set_content(text_body)
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=f"ponuda_{offer_no}.pdf")

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as s:
            if smtp_tls:
                s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        db.mark_offer_sent(user_id, username, offer_id)
    except Exception as e:
        return RedirectResponse(url=f"/offer?err=Neuspjelo+slanje:+{str(e).replace(' ', '+')}", status_code=HTTP_303_SEE_OTHER)

    return RedirectResponse(url="/offer?ok=Ponuda+poslana+(SENT)", status_code=HTTP_303_SEE_OTHER)


@app.get("/offers", response_class=HTMLResponse)
def offers_page(request: Request):
    username, user_id = _user_ctx(request)
    show = request.query_params.get("show") or "active"
    offers = db.list_offers(user_id, username, show=show)
    return templates.TemplateResponse(
        "offers.html",
        {
            "request": request,
            "user": username,
            "offers": offers,
            "show": show,
            "err": request.query_params.get("err"),
            "ok": request.query_params.get("ok"),
        },
    )



@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    username, user_id = _user_ctx(request)
    year = request.query_params.get("year")
    try:
        yr = int(year) if year else datetime.now().year
    except Exception:
        yr = datetime.now().year
    monthly = db.dashboard_monthly(user_id, username, year=yr)
    # Totals
    subtotal = sum(float(m.get("subtotal") or 0) for m in monthly)
    vat = subtotal * 0.25
    total = subtotal + vat
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": username,
            "year": yr,
            "monthly": monthly,
            "subtotal": subtotal,
            "vat": vat,
            "total": total,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    username, user_id = _user_ctx(request)
    settings = db.get_settings(user_id, username)
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "user": username, "settings": settings, "ok": request.query_params.get("ok")},
    )


@app.post("/settings")
async def settings_save(
    request: Request,
    company_name: str = Form(""),
    company_address: str = Form(""),
    company_oib: str = Form(""),
    company_iban: str = Form(""),
    company_email: str = Form(""),
    company_phone: str = Form(""),
    logo_path: str = Form(""),
    logo_file: UploadFile | None = File(None),
):
    username, user_id = _user_ctx(request)

    logo_bytes = None
    logo_mime = None
    logo_filename = None
    if logo_file and logo_file.filename:
        # Limit: 2MB
        data = await logo_file.read()
        if data and len(data) > 2 * 1024 * 1024:
            return RedirectResponse(url="/settings?ok=0", status_code=HTTP_303_SEE_OTHER)
        logo_bytes = data or None
        logo_mime = (logo_file.content_type or "").strip() or None
        logo_filename = (logo_file.filename or "").strip() or None

    db.upsert_settings(
        user_id=user_id,
        username=username,
        company_name=(company_name or "").strip() or None,
        company_address=(company_address or "").strip() or None,
        company_oib=(company_oib or "").strip() or None,
        company_iban=(company_iban or "").strip() or None,
        company_email=(company_email or "").strip() or None,
        company_phone=(company_phone or "").strip() or None,
        logo_path=(logo_path or "").strip() or None,
        logo_bytes=logo_bytes,
        logo_mime=logo_mime,
        logo_filename=logo_filename,
    )
    return RedirectResponse(url="/settings?ok=1", status_code=HTTP_303_SEE_OTHER)


@app.post("/settings/logo/clear")
def settings_logo_clear(request: Request):
    username, user_id = _user_ctx(request)
    db.clear_logo(user_id, username)
    return RedirectResponse(url="/settings?ok=1", status_code=HTTP_303_SEE_OTHER)



@app.get("/backup/export")
def backup_export(request: Request):
    username, user_id = _user_ctx(request)
    data = db.export_user_backup_zip(user_id, username, static_dir=str(STATIC_DIR))
    fname = f"ponude_backup_{username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/__routes")
def debug_routes():
    return {"routes": [getattr(r, "path", str(r)) for r in app.router.routes]}
