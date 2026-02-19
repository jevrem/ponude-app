from __future__ import annotations

import os
import html
import smtplib
import json
import base64
import urllib.request
import urllib.error
from email.message import EmailMessage
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Tuple

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from . import db
from .security import require_login, verify_credentials, logout, require_admin, is_admin


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI()

# Static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# MUST be set in Render env vars for production
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()




import logging
logger = logging.getLogger("ponude")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

@app.exception_handler(Exception)
async def _unhandled_exc(request: Request, exc: Exception):
    # log to stdout + audit (best-effort)
    try:
        logger.exception("Unhandled error: %s %s", request.method, request.url.path)
    except Exception:
        pass
    try:
        user = request.session.get("user")
        # user_id might not be available; keep None
        db.log_audit(None, (str(user) if user else None), "error", None, ip=request.client.host if request.client else None, meta={"path": str(request.url.path), "err": str(exc)})
    except Exception:
        pass
    return HTMLResponse("Internal Server Error", status_code=500)

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



def _client_ip(request: Request) -> str | None:
    # Render/Proxy: X-Forwarded-For may contain multiple IPs
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _time_ago(dt) -> str:
    if not dt:
        return ""
    try:
        # dt may be string or datetime
        if isinstance(dt, str):
            # best-effort parse
            from datetime import datetime
            try:
                dt_obj = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            except Exception:
                return ""
        else:
            dt_obj = dt
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc) if getattr(dt_obj, "tzinfo", None) else datetime.now()
        delta = now - dt_obj
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"prije {secs}s"
        mins = secs // 60
        if mins < 60:
            return f"prije {mins} min"
        hrs = mins // 60
        if hrs < 24:
            return f"prije {hrs} h"
        days = hrs // 24
        return f"prije {days} d"
    except Exception:
        return ""

def _ensure_offer(request: Request, username: str, user_id: int) -> int:
    oid = _get_offer_id(request)
    if oid is not None:
        off = db.get_offer(user_id, username, oid)
        if off:
            return oid
    new_id = db.create_offer(user_id, username, None)
    request.session["offer_id"] = int(new_id)
    return int(new_id)




@app.get("/catalog")
def catalog_alias(request: Request):
    # Backward-compatible alias for older templates
    require_login(request)
    return RedirectResponse(url="/settings", status_code=HTTP_303_SEE_OTHER)
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
        uid = db.ensure_user(username.strip().lower())
        db.log_audit(uid, username.strip().lower(), "login", ip=_client_ip(request))
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("login.html", {"request": request, "err": "Pogrešan korisnik ili lozinka."})


@app.post("/logout")
def logout_post(request: Request):
    try:
        u = request.session.get("user")
        if u:
            uid = db.ensure_user(str(u).strip().lower())
            db.log_audit(uid, str(u).strip().lower(), "logout", ip=_client_ip(request))
    except Exception:
        pass
    logout(request)
    return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)


@app.get("/offer", response_class=HTMLResponse)
def offer_page(request: Request):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)
    offer = dict(db.get_offer(user_id, username, offer_id) or {})
    if offer.get("vat_rate") is None:
        offer["vat_rate"] = 0
    items = db.list_items(offer_id)
    settings = db.get_settings(user_id, username)

    # Public portal token/link for client view + tracking
    token = None
    portal_url = None
    try:
        token = db.ensure_public_token(user_id, username, offer_id)
        portal_url = str(request.base_url).rstrip("/") + f"/p/{token}"
    except Exception:
        token = None
        portal_url = None

    subtotal = sum(float(it.get("line_total") or 0) for it in items)
    vat_rate = float(offer.get('vat_rate') or 0)
    vat = subtotal * (vat_rate / 100.0) if vat_rate else 0.0
    total = subtotal + vat

    smtp_host = (os.getenv("SMTP_HOST") or "").strip()
    smtp_user = (os.getenv("SMTP_USER") or "").strip()
    smtp_pass = (os.getenv("SMTP_PASS") or "").strip()
    smtp_configured = bool(smtp_host and smtp_user and smtp_pass)

    brevo_key = (os.getenv("BREVO_API_KEY") or "").strip()
    brevo_from = (os.getenv("BREVO_FROM_EMAIL") or "").strip()
    brevo_configured = bool(brevo_key and brevo_from)

    # UI expects smtp_configured flag; treat Brevo as configured too
    smtp_configured = smtp_configured or brevo_configured

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
            "smtp_configured": smtp_configured,
            "portal_url": portal_url,
            "clients": db.list_clients_full(user_id, username),
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


@app.get("/offers/pdf")
def offers_pdf(request: Request, offer_id: int):
    username = require_admin(request).strip().lower()
    user_id = db.ensure_user(username)
    request.session["offer_id"] = int(offer_id)
    return RedirectResponse(url="/offer/pdf", status_code=HTTP_303_SEE_OTHER)


@app.get("/offers/portal")
def offers_portal(request: Request, offer_id: int):
    username = require_admin(request).strip().lower()
    user_id = db.ensure_user(username)
    token = db.ensure_public_token(user_id, username, int(offer_id))
    return RedirectResponse(url=f"/p/{token}", status_code=HTTP_303_SEE_OTHER)

    request.session["offer_id"] = int(offer_id)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/client")
def offer_client(
    request: Request,
    client_name: str = Form(""),
    client_email: str = Form(""),
    client_address: str = Form(""),
    client_oib: str = Form(""),
):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)
    try:
        db.update_offer_client_details(user_id, username, offer_id, (client_name or "").strip() or None, client_email, client_address, client_oib)
    except Exception as e:
        return RedirectResponse(url=f"/offer?err={str(e).replace(' ', '+')}", status_code=HTTP_303_SEE_OTHER)
    # Keep a lightweight client list
    try:
        db.upsert_client_full(user_id, username, (client_name or "").strip(), email=client_email, address=client_address, oib=client_oib)
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
    vat_rate: float = Form(0),
    valid_until: str = Form(""),
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
            vat_rate=float(vat_rate or 0),
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


@app.post("/offer/duplicate")
def offer_duplicate(request: Request, offer_id: int = Form(...)):
    username = require_admin(request).strip().lower()
    user_id = db.ensure_user(username)
    try:
        new_id = db.duplicate_offer(user_id, username, int(offer_id))
        request.session["offer_id"] = int(new_id)
        return RedirectResponse(url="/offer?ok=Duplicirano", status_code=HTTP_303_SEE_OTHER)
    except Exception as e:
        return RedirectResponse(url="/offers?err=" + str(e), status_code=HTTP_303_SEE_OTHER)


@app.get("/offer/pdf")
def offer_pdf(request: Request):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)

    offer = dict(db.get_offer(user_id, username, offer_id) or {})
    if offer.get("vat_rate") is None:
        offer["vat_rate"] = 0
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
    if offer.get("vat_rate") is None:
        offer["vat_rate"] = 0
    items = db.list_items(offer_id)

    xls_bytes = db.render_offer_excel(offer=offer, items=items)
    fname = f"ponuda_{username}_{offer.get('offer_no') or offer_id}.xlsx"

    return Response(
        content=xls_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )




# -----------------------------
# Public portal + tracking
# -----------------------------

_PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)

@app.get("/t/open/{token}.gif")
def track_open(token: str, request: Request):
    try:
        db.track_open(token, ip=_client_ip(request))
    except Exception:
        pass
    return Response(content=_PIXEL_GIF, media_type="image/gif", headers={"Cache-Control": "no-store"})

@app.get("/t/click/{token}")
def track_click(token: str, request: Request):
    try:
        db.track_click(token, ip=_client_ip(request))
    except Exception:
        pass
    return RedirectResponse(url=f"/p/{token}", status_code=HTTP_303_SEE_OTHER)

@app.get("/p/{token}", response_class=HTMLResponse)
def portal_page(token: str, request: Request):
    off = db.get_offer_by_token(token)
    if not off:
        return HTMLResponse("Not found", status_code=404)
    offer_id = int(off["id"])
    items = db.list_items(offer_id)
    # Render minimal portal view (no auth)
    subtotal = sum(float(it.get("line_total") or 0) for it in items)
    vat_rate = float(off.get("vat_rate") or 0)
    vat = subtotal * (vat_rate / 100.0) if vat_rate else 0.0
    total = subtotal + vat
    return templates.TemplateResponse(
        "portal.html",
        {
            "request": request,
            "offer": off,
            "items": items,
            "subtotal": subtotal,
            "vat": vat,
            "total": total,
            "token": token,
            "ok": request.query_params.get("ok"),
            "err": request.query_params.get("err"),
        },
    )

@app.post("/p/{token}/accept")
def portal_accept(token: str, request: Request):
    try:
        changed = db.accept_by_token(token, ip=_client_ip(request))
    except Exception as e:
        print("PORTAL_ACCEPT DB ERROR:", repr(e))
        return RedirectResponse(url=f"/p/{token}?err=Greška+pri+potvrdi", status_code=HTTP_303_SEE_OTHER)

    if changed:
        err_msg = None
        try:
            off = db.get_offer_by_token(token) or {}
            offer_id = int(off.get("id") or 0)
            offer_no = off.get("offer_no") or str(offer_id)
            client_name = off.get("client_name") or ""
            client_email = (off.get("client_email") or "").strip()
            base = str(request.base_url).rstrip("/")
            portal_url = f"{base}/p/{token}"
            pdf_url = f"{base}/p/{token}/pdf"

            api_key = (os.getenv("BREVO_API_KEY") or "").strip()
            from_email = (os.getenv("BREVO_FROM_EMAIL") or "").strip()
            from_name = (os.getenv("BREVO_FROM_NAME") or "").strip() or "Ponude"
            notify_to = (os.getenv("ADMIN_NOTIFY_EMAIL") or "").strip() or from_email

            if not (api_key and from_email and notify_to):
                err_msg = "Email+notifikacije+niso+konfigurirane"
            else:
                subj_admin = f"✅ Ponuda {offer_no} prihvaćena"
                html_admin = (
                    f"<p>Ponuda <b>{offer_no}</b> je prihvaćena.</p>"
                    f"<p><b>Klijent:</b> {client_name}<br>"
                    f"<b>Email:</b> {client_email}</p>"
                    f"<p><a href='{portal_url}'>Otvori portal</a> • <a href='{pdf_url}'>PDF</a></p>"
                )
                txt_admin = f"Ponuda {offer_no} je prihvaćena.\nKlijent: {client_name}\nEmail: {client_email}\nPortal: {portal_url}\nPDF: {pdf_url}"
                try:
                    _send_brevo(api_key, from_email, from_name, notify_to, subj_admin, html_admin, txt_admin)
                except Exception as e:
                    print("PORTAL_ACCEPT ADMIN EMAIL ERROR:", repr(e))
                    err_msg = "Admin+email+neuspješan"

                if client_email and not err_msg:
                    subj_client = f"Hvala! Ponuda {offer_no} je potvrđena"
                    html_client = (
                        f"<p>Hvala na potvrdi ponude <b>{offer_no}</b>.</p>"
                        f"<p>Možete ponovno otvoriti ponudu ovdje: <a href='{portal_url}'>Portal</a>.</p>"
                        f"<p>PDF: <a href='{pdf_url}'>Preuzmi</a></p>"
                    )
                    txt_client = f"Hvala na potvrdi ponude {offer_no}.\nPortal: {portal_url}\nPDF: {pdf_url}"
                    try:
                        _send_brevo(api_key, from_email, from_name, client_email, subj_client, html_client, txt_client)
                    except Exception as e:
                        print("PORTAL_ACCEPT CLIENT EMAIL ERROR:", repr(e))
                        err_msg = "Kupac+email+neuspješan"
        except Exception as e:
            print("PORTAL_ACCEPT UNEXPECTED ERROR:", repr(e))
            err_msg = "Neuspjela+notifikacija"

        if err_msg:
            return RedirectResponse(url=f"/p/{token}?ok=Ponuda+potvrđena&err={err_msg}", status_code=HTTP_303_SEE_OTHER)
        return RedirectResponse(url=f"/p/{token}?ok=Ponuda+potvrđena", status_code=HTTP_303_SEE_OTHER)

    return RedirectResponse(url=f"/p/{token}?ok=Već+potvrđeno", status_code=HTTP_303_SEE_OTHER)
@app.get("/p/{token}/pdf")
def portal_pdf(token: str, request: Request):
    off = db.get_offer_by_token(token)
    if not off:
        return HTMLResponse("Not found", status_code=404)
    offer_id = int(off["id"])
    items = db.list_items(offer_id)
    # settings for rendering: use stored per-user settings when possible
    username = (off.get("user_name") or "user").strip().lower()
    user_id = int(off.get("user_id") or db.ensure_user(username))
    settings = db.get_settings(user_id, username)
    logo_bytes, _mime = db.get_logo_bytes(user_id, username)
    pdf_bytes = db.render_offer_pdf(offer=dict(off), items=items, settings=settings, static_dir=str(STATIC_DIR), logo_bytes=logo_bytes)
    fname = f"ponuda_{off.get('offer_no') or offer_id}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{fname}"'})


def _send_brevo(api_key: str, from_email: str, from_name: str, to_email: str, subject: str, html_body: str, text_body: str = "", attachment_pdf: bytes | None = None, attachment_name: str = "ponuda.pdf") -> None:
    """Send transactional email via Brevo HTTPS API (port 443)."""
    import urllib.request, urllib.error
    payload: dict = {
        "sender": {"email": from_email, "name": from_name or from_email},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_body,
    }
    if text_body:
        payload["textContent"] = text_body
    if attachment_pdf is not None:
        payload["attachment"] = [{
            "content": base64.b64encode(attachment_pdf).decode("ascii"),
            "name": attachment_name,
        }]
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=data,
        headers={"Content-Type": "application/json", "api-key": api_key},
        method="POST",
    )
    # Force IPv4 DNS resolution to avoid environments with no IPv6 route
    import socket as _socket
    _orig_getaddrinfo = _socket.getaddrinfo
    def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)
    _socket.getaddrinfo = _ipv4_getaddrinfo
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            _ = resp.read()
            if not (200 <= resp.status < 300):
                raise Exception(f"Brevo HTTP {resp.status}")
    finally:
        _socket.getaddrinfo = _orig_getaddrinfo

@app.post("/offer/send")
def offer_send(
    request: Request,
    to_email: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
):
    username, user_id = _user_ctx(request)
    offer_id = _ensure_offer(request, username, user_id)

    offer = dict(db.get_offer(user_id, username, offer_id) or {})
    items = db.list_items(offer_id)
    settings = db.get_settings(user_id, username)
    logo_bytes, _mime = db.get_logo_bytes(user_id, username)

    recipient = (to_email or "").strip() or (offer.get("client_email") or "").strip()
    if not recipient:
        return RedirectResponse(url="/offer?err=Nedostaje+email+klijenta", status_code=HTTP_303_SEE_OTHER)

    # Persist client email (if offer is editable)
    try:
        db.update_offer_client_email(user_id, username, offer_id, recipient)
    except Exception:
        pass

    smtp_host = (os.getenv("SMTP_HOST") or "").strip()
    smtp_user = (os.getenv("SMTP_USER") or "").strip()
    smtp_pass = (os.getenv("SMTP_PASS") or "").strip()
    smtp_port = int(os.getenv("SMTP_PORT") or "587")
    smtp_tls = (os.getenv("SMTP_TLS") or "1").strip() not in {"0", "false", "False", "no", "NO"}

    brevo_key = (os.getenv("BREVO_API_KEY") or "").strip()
    brevo_from = (os.getenv("BREVO_FROM_EMAIL") or "").strip()
    brevo_from_name = (os.getenv("BREVO_FROM_NAME") or "").strip()
    brevo_reply_to = (os.getenv("BREVO_REPLY_TO") or "").strip()

    if not (brevo_key and brevo_from) and (not smtp_host or not smtp_user or not smtp_pass):
        return RedirectResponse(url="/offer?err=Email+nije+konfiguriran+(BREVO_API_KEY/BREVO_FROM_EMAIL+ili+SMTP_HOST/SMTP_USER/SMTP_PASS)", status_code=HTTP_303_SEE_OTHER)

    pdf_bytes = db.render_offer_pdf(offer=offer, items=items, settings=settings, static_dir=str(STATIC_DIR), logo_bytes=logo_bytes)
    offer_no = offer.get("offer_no") or str(offer_id)
    client_name = (offer.get("client_name") or "").strip() or "klijent"

    tpls = db.get_templates(user_id, username)
    ctx = {
    "offer_no": offer_no,
    "client_name": client_name,
    "company_name": settings.get("company_name") or username,
    }
    subj = subject.strip() or (tpls.get("email_subject_tpl") or "").format(**ctx)
    text_body = body.strip() or (tpls.get("email_text_tpl") or "").format(**ctx)
    html_body = (tpls.get("email_html_tpl") or "").format(
    offer_no=html.escape(str(offer_no)),
    client_name=html.escape(client_name),
    company_name=html.escape(str(settings.get("company_name") or username)),
    )

    # Prepend branding (logo) to HTML email (served from /static)
    try:
        base = str(request.base_url).rstrip("/")
        logo_url = base + "/static/logo_mail.png"
        html_body = (
            f'<div style="margin-bottom:16px">'
            f'<img src="{logo_url}" alt="{html.escape(str(settings.get("company_name") or username))}" style="max-width:240px;height:auto" />'
            f'</div>'
        ) + html_body
    except Exception:
        pass

    # Add portal link + tracking pixel (works even if template is custom)
    try:
        token = db.ensure_public_token(user_id, username, offer_id)
        portal_url = str(request.base_url).rstrip("/") + f"/p/{token}"
        click_url = str(request.base_url).rstrip("/") + f"/t/click/{token}"
        open_url = str(request.base_url).rstrip("/") + f"/t/open/{token}.gif"
        if portal_url and portal_url not in text_body:
            text_body = text_body + "\n\nPregled ponude i potvrda: " + portal_url
        # For clicks we use tracking redirect
        if "href=" in html_body and "t/click" not in html_body:
            html_body = html_body + f'<p style="margin-top:16px"><a href="{click_url}">Pregled ponude / potvrda</a></p>'
        html_body = html_body + f'<img src="{open_url}" width="1" height="1" alt="" style="display:none" />'
    except Exception:
        pass
    # PRO email CTA + tracking + plain portal URL (always)
    try:
        token = db.ensure_public_token(user_id, username, offer_id)
        base = str(request.base_url).rstrip("/")
        portal_url = base + "/p/" + token
        click_url = base + "/t/click/" + token
        open_url = base + "/t/open/" + token + ".gif"

        html_body = html_body + (
            '<div style="margin-top:24px;padding:16px;border:1px solid #e2e8f0;'
            'border-radius:12px;background:#f8fafc">'
            '<div style="font-size:16px;font-weight:600;margin-bottom:8px">'
            'Pregled i potvrda ponude</div>'
            '<a href="' + click_url + '" style="display:inline-block;background:#16a34a;color:#fff;'
            'padding:12px 18px;border-radius:10px;text-decoration:none;font-weight:600">'
            'Otvori ponudu</a></div>'
            '<img src="' + open_url + '" width="1" height="1" style="display:none" />'
        )
        if portal_url not in text_body:
            text_body = text_body + "\n\nPregled ponude: " + portal_url
    except Exception:
        pass


    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg["Subject"] = subj
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=f"ponuda_{offer_no}.pdf")

    try:
        if brevo_key and brevo_from:
            # Brevo (Messaging API) over HTTPS (port 443) - avoids SMTP egress blocks
            payload = {
                "sender": {"email": brevo_from},
                "to": [{"email": recipient}],
                "subject": subj,
                "textContent": text_body,
                "htmlContent": html_body,
                "attachment": [
                    {"content": base64.b64encode(pdf_bytes).decode("ascii"), "name": f"ponuda_{offer_no}.pdf"}
                ],
            }
            if brevo_from_name:
                payload["sender"]["name"] = brevo_from_name
            if client_name:
                payload["to"][0]["name"] = client_name
            if brevo_reply_to:
                payload["replyTo"] = {"email": brevo_reply_to}

            req = urllib.request.Request(
                url="https://api.brevo.com/v3/smtp/email",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "api-key": brevo_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                # Force IPv4 DNS resolution to avoid environments with no IPv6 route
                import socket as _socket
                _orig_getaddrinfo = _socket.getaddrinfo
                def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
                    return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)
                _socket.getaddrinfo = _ipv4_getaddrinfo
                try:
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        _ = resp.read()  # may contain messageId json
                        if not (200 <= resp.status < 300):
                            raise Exception(f"Brevo HTTP {resp.status}")
                finally:
                    _socket.getaddrinfo = _orig_getaddrinfo
            except urllib.error.HTTPError as he:
                body_bytes = he.read() if hasattr(he, "read") else b""
                body_txt = body_bytes.decode("utf-8", errors="replace") if body_bytes else ""
                raise Exception(f"Brevo HTTP {he.code}: {body_txt[:300]}")
            except urllib.error.URLError as ue:
                raise Exception(f"Brevo URL error: {ue}")
        else:
            # SMTP fallback (local/dev)
            with smtplib.SMTP(smtp_host, smtp_port, timeout=25) as s:
                if smtp_tls:
                    s.starttls()
                s.login(smtp_user, smtp_pass)
                s.send_message(msg)

        db.mark_offer_sent(user_id, username, offer_id)
        db.record_email_result(user_id, username, offer_id, recipient, ok=True, error=None)
        db.log_audit(user_id, username, "email_send", offer_id=offer_id, ip=_client_ip(request), meta={"to": recipient})
    except Exception as e:
        db.record_email_result(user_id, username, offer_id, recipient, ok=False, error=str(e))
        db.log_audit(user_id, username, "email_send_failed", offer_id=offer_id, ip=_client_ip(request), meta={"to": recipient, "err": str(e)})
        return RedirectResponse(url=f"/offer?err=Neuspjelo+slanje:+{str(e).replace(' ', '+')}", status_code=HTTP_303_SEE_OTHER)

    return RedirectResponse(url="/offer?ok=Email+poslan", status_code=HTTP_303_SEE_OTHER)


@app.get("/offers"
, response_class=HTMLResponse)
def offers_page(request: Request):
    username, user_id = _user_ctx(request)
    show = request.query_params.get("show") or "active"
    status = request.query_params.get("status") or "ALL"
    invoice = request.query_params.get("invoice") or "ALL"
    paid = request.query_params.get("paid") or "ALL"
    client = request.query_params.get("client") or "ALL"
    q = request.query_params.get("q") or ""
    offers = db.list_offers(user_id, username, show=show, status=status, invoice=invoice, paid=paid, client=client, q=q)

    # Enrich offers with view/click info
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    for o in offers:
        lv = o.get("last_view_at")
        o["view_ago"] = _time_ago(lv) if lv else ""
        # "live" if viewed in last 3 minutes
        try:
            if lv and isinstance(lv, str):
                lv_dt = datetime.fromisoformat(lv.replace("Z", "+00:00"))
            else:
                lv_dt = lv
            o["is_live"] = bool(lv_dt and (now - lv_dt) <= timedelta(minutes=3))
        except Exception:
            o["is_live"] = False


        # Expiry badges
        vu = o.get("valid_until")
        o["expiry_label"] = ""
        o["is_expired"] = False
        o["is_expires_today"] = False
        o["is_expires_soon"] = False
        try:
            from datetime import date as _date, datetime as _dt
            if isinstance(vu, str) and vu:
                # Accept YYYY-MM-DD
                vu_d = _dt.fromisoformat(vu).date() if "T" in vu else _dt.strptime(vu[:10], "%Y-%m-%d").date()
            elif hasattr(vu, "year"):
                vu_d = vu
            else:
                vu_d = None
            if vu_d:
                today = _date.today()
                if vu_d < today:
                    o["is_expired"] = True
                    o["expiry_label"] = "Istekla"
                elif vu_d == today:
                    o["is_expires_today"] = True
                    o["expiry_label"] = "Istječe danas"
                elif (vu_d - today).days <= 3:
                    o["is_expires_soon"] = True
                    o["expiry_label"] = f"Istječe za {(vu_d - today).days}d"
                else:
                    o["expiry_label"] = f"Vrijedi do {vu_d.strftime('%d.%m.%Y')}"
        except Exception:
            pass

    # Stats for header (current filter)
    stats = {
        "total_count": len(offers),
        "draft": 0,
        "sent": 0,
        "accepted": 0,
        "opened": 0,
        "expired": 0,
        "sum_total": 0.0,
    }
    for o in offers:
        st = (o.get("status") or "DRAFT").upper()
        if o.get("is_expired"):
            stats["expired"] += 1
        if st == "ACCEPTED":
            stats["accepted"] += 1
        elif st == "SENT":
            stats["sent"] += 1
        elif st == "DRAFT":
            stats["draft"] += 1
        else:
            # treat any other statuses as draft-ish
            stats["draft"] += 1
        if (o.get("view_count") or 0) or o.get("last_view_at"):
            stats["opened"] += 1
        try:
            stats["sum_total"] += float(o.get("total") or 0)
        except Exception:
            pass

    return templates.TemplateResponse(
        "offers.html",
        {
            "request": request,
            "user": username,
            "offers": offers,
            "stats": stats,
            "show": show,
            "status": (status or "ALL").upper(),
            "invoice": (invoice or "ALL").upper(),
            "paid": (paid or "ALL").upper(),
            "client": client,
            "clients": db.list_clients_full(user_id, username),
            "q": q,
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
        {"request": request, "user": username, "settings": settings, "templates": db.get_templates(user_id, username), "ok": request.query_params.get("ok")},
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
    email_subject_tpl: str = Form(""),
    email_text_tpl: str = Form(""),
    email_html_tpl: str = Form(""),
    pdf_footer_tpl: str = Form(""),
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
    db.set_templates(
    user_id=user_id,
    username=username,
    subject=(email_subject_tpl or "").strip() or db.DEFAULT_EMAIL_SUBJECT,
    text_t=(email_text_tpl or "").strip() or db.DEFAULT_EMAIL_TEXT,
    html_t=(email_html_tpl or "").strip() or db.DEFAULT_EMAIL_HTML,
    footer=(pdf_footer_tpl or "").strip(),
    )
    db.log_audit(user_id, username, "settings_update", ip=_client_ip(request))
    return RedirectResponse(url="/settings?ok=1", status_code=HTTP_303_SEE_OTHER)


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    username, user_id = _user_ctx(request)
    # Admin only (same user)
    require_admin(request)
    logs = db.list_audit(limit=300)
    return templates.TemplateResponse(
        "logs.html",
        {"request": request, "user": username, "logs": logs},
    )



@app.post("/settings/logo/clear")
def settings_logo_clear(request: Request):
    username, user_id = _user_ctx(request)
    db.clear_logo(user_id, username)
    return RedirectResponse(url="/settings?ok=1", status_code=HTTP_303_SEE_OTHER)



@app.post("/settings/smtp-test")
def smtp_test(request: Request, to_email: str = Form("")):
    # Admin-only
    require_admin(request)
    username, user_id = _user_ctx(request)

    smtp_host = (os.getenv("SMTP_HOST") or "").strip()
    smtp_user = (os.getenv("SMTP_USER") or "").strip()
    smtp_pass = (os.getenv("SMTP_PASS") or "").strip()
    smtp_port = int(os.getenv("SMTP_PORT") or "587")
    smtp_tls = (os.getenv("SMTP_TLS") or "1").strip() not in {"0", "false", "False", "no", "NO"}

    if not smtp_host or not smtp_user or not smtp_pass:
        return RedirectResponse(url="/settings?err=SMTP+nije+konfiguriran+(SMTP_HOST/SMTP_USER/SMTP_PASS)", status_code=HTTP_303_SEE_OTHER)

    dest = (to_email or "").strip() or smtp_user
    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = dest
    msg["Subject"] = "PonudeApp SMTP test"
    msg.set_content(f"SMTP test uspješan. Vrijeme: {datetime.now().isoformat(timespec='seconds')}")

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=25) as s:
            if smtp_tls:
                s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        db.log_audit(user_id, username, "smtp_test_ok", ip=_client_ip(request), meta={"to": dest})
        return RedirectResponse(url="/settings?ok=SMTP+test+poslan", status_code=HTTP_303_SEE_OTHER)
    except Exception as e:
        db.log_audit(user_id, username, "smtp_test_failed", ip=_client_ip(request), meta={"to": dest, "err": str(e)})
        return RedirectResponse(url=f"/settings?err=SMTP+test+nije+uspio:+{str(e).replace(' ', '+')}", status_code=HTTP_303_SEE_OTHER)


@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request):
    username, user_id = _user_ctx(request)
    clients = db.list_clients_full(user_id, username)
    return templates.TemplateResponse(
        "clients.html",
        {
            "request": request,
            "user": username,
            "clients": clients,
            "ok": request.query_params.get("ok"),
            "err": request.query_params.get("err"),
        },
    )


@app.post("/clients/upsert")
def clients_upsert(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    address: str = Form(""),
    oib: str = Form(""),
    note: str = Form(""),
):
    username, user_id = _user_ctx(request)
    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(url="/clients?err=Naziv+klijenta+je+obavezan", status_code=HTTP_303_SEE_OTHER)
    try:
        db.upsert_client_full(user_id, username, nm, email=email, address=address, oib=oib, note=note)
        db.log_audit(user_id, username, "client_upsert", ip=_client_ip(request), meta={"name": nm})
    except Exception as e:
        return RedirectResponse(url=f"/clients?err={str(e).replace(' ', '+')}", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/clients?ok=Spremljeno", status_code=HTTP_303_SEE_OTHER)

@app.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request):
    username = require_admin(request).strip().lower()
    user_id = db.ensure_user(username)
    return templates.TemplateResponse(
        "backup.html",
        {
            "request": request,
            "user": username,
            "err": request.query_params.get("err"),
            "ok": request.query_params.get("ok"),
        },
    )


@app.post("/backup/import")
async def backup_import(
    request: Request,
    file: UploadFile = File(...),
    restore_as_archived: int = Form(1),
):
    username = require_admin(request).strip().lower()
    user_id = db.ensure_user(username)
    try:
        data = await file.read()
        payload = None
        name = (file.filename or "").lower()
        if name.endswith(".zip"):
            import zipfile, io, json
            with zipfile.ZipFile(io.BytesIO(data), "r") as z:
                with z.open("offers.json") as f:
                    payload = json.loads(f.read().decode("utf-8"))
        else:
            import json
            payload = json.loads(data.decode("utf-8"))
        stats = db.import_user_backup(user_id, username, payload, restore_as_archived=bool(int(restore_as_archived)))
        db.log_audit(user_id, username, "backup_import", ip=_client_ip(request), meta=stats)
        return RedirectResponse(url=f"/backup?ok=Importirano: {stats.get('imported',0)}", status_code=HTTP_303_SEE_OTHER)
    except Exception as e:
        return RedirectResponse(url="/backup?err=" + str(e), status_code=HTTP_303_SEE_OTHER)


@app.get("/backup/export")
def backup_export(request: Request):
    username, user_id = _user_ctx(request)
    data = db.export_user_backup_zip(user_id, username, static_dir=str(STATIC_DIR))
    db.log_audit(user_id, username, "backup_export", ip=_client_ip(request))
    fname = f"ponude_backup_{username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/__routes")
def debug_routes():
    return {"routes": [getattr(r, "path", str(r)) for r in app.router.routes]}


# -----------------------------
# Invoice
# -----------------------------

@app.post("/invoice/create")
def invoice_create(request: Request):
    username, user_id = _user_ctx(request)
    offer_id = _get_offer_id(request)
    if not offer_id:
        return RedirectResponse(url="/offer?err=Nema+ponude", status_code=HTTP_303_SEE_OTHER)
    try:
        off = db.create_invoice_from_offer(user_id, username, int(offer_id))
        db.log_audit(user_id, username, "invoice_create", offer_id=int(offer_id), ip=_client_ip(request), meta={"invoice_no": off.get("invoice_no")})
        return RedirectResponse(url="/offer?ok=invoice", status_code=HTTP_303_SEE_OTHER)
    except Exception as e:
        return RedirectResponse(url="/offer?err=" + html.escape(str(e)), status_code=HTTP_303_SEE_OTHER)


@app.get("/invoice/pdf")
def invoice_pdf(request: Request):
    username, user_id = _user_ctx(request)
    oid_q = request.query_params.get("offer_id")
    offer_id = int(oid_q) if oid_q and str(oid_q).isdigit() else _get_offer_id(request)
    if not offer_id:
        return RedirectResponse(url="/offer?err=Nema+ponude", status_code=HTTP_303_SEE_OTHER)
    offer = dict(db.get_offer(user_id, username, int(offer_id)) or {})
    if not offer.get("invoice_no"):
        return RedirectResponse(url="/offer?err=Nema+računa+za+ovu+ponudu", status_code=HTTP_303_SEE_OTHER)
    items = db.list_items(int(offer_id))
    settings = db.get_settings(user_id, username)
    logo_bytes, _mime = db.get_logo_bytes(user_id, username)
    pdf = db.render_invoice_pdf(offer=offer, items=items, settings=settings, static_dir=str(STATIC_DIR), logo_bytes=logo_bytes)
    db.log_audit(user_id, username, "invoice_pdf", offer_id=int(offer_id), ip=_client_ip(request))
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=Racun_{offer.get('invoice_no')}.pdf"})


@app.post("/invoice/paid")
def invoice_paid(request: Request, paid: str = Form("0"), offer_id: int | None = Form(None)):
    username, user_id = _user_ctx(request)
    oid = int(offer_id) if offer_id else _get_offer_id(request)
    offer_id = oid
    if not offer_id:
        return RedirectResponse(url="/offer?err=Nema+ponude", status_code=HTTP_303_SEE_OTHER)
    val = str(paid).strip() in {"1", "true", "True", "yes", "YES", "on"}
    try:
        db.set_invoice_paid(user_id, username, int(offer_id), val)
        db.log_audit(user_id, username, "invoice_paid_set", offer_id=int(offer_id), ip=_client_ip(request), meta={"paid": val})
        return RedirectResponse(url="/offer?ok=paid", status_code=HTTP_303_SEE_OTHER)
    except Exception as e:
        return RedirectResponse(url="/offer?err=" + html.escape(str(e)), status_code=HTTP_303_SEE_OTHER)



# -----------------------------
# Admin
# -----------------------------

@app.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(request: Request):
    admin = require_admin(request).strip().lower()
    rows = db.list_audit(limit=250)
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": admin, "audit_rows": rows, "admin_view": "audit"})


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request):
    admin = require_admin(request).strip().lower()
    # reuse dashboard template in a minimal way (no extra template files)
    with db.get_conn() as conn:
        users = conn.execute("select id, username, created_at from users order by id asc").fetchall()
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": admin, "admin_view": "users", "users": [dict(u) for u in users]})


@app.post("/admin/users/create")
def admin_users_create(request: Request, username: str = Form(...)):
    admin = require_admin(request).strip().lower()
    u = (username or "").strip().lower()
    if not u:
        return RedirectResponse(url="/admin/users", status_code=HTTP_303_SEE_OTHER)
    uid = db.ensure_user(u)
    db.log_audit(db.ensure_user(admin), admin, "admin_create_user", ip=_client_ip(request), meta={"user": u, "id": uid})
    return RedirectResponse(url="/admin/users", status_code=HTTP_303_SEE_OTHER)

