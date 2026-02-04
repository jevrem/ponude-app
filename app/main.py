import os
from io import BytesIO
from datetime import datetime

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# IMPORTANT: define app first so uvicorn can always find it
app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret-change-me")

COMPANY_NAME = os.getenv("COMPANY_NAME", "Amaryllis interijeri")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "")
COMPANY_OIB = os.getenv("COMPANY_OIB", "")
COMPANY_IBAN = os.getenv("COMPANY_IBAN", "")
COMPANY_EMAIL = os.getenv("COMPANY_EMAIL", "")
COMPANY_PHONE = os.getenv("COMPANY_PHONE", "")
COMPANY_LOGO_PATH = os.getenv("COMPANY_LOGO_PATH", "app/static/logo.png")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=False,  # set True if you want to force secure cookies
)

_db_import_error = None
_security_import_error = None

try:
    from .db import (
        init_db,
        create_offer,
        get_offer,
        update_offer_client_name,
        list_items,
        add_item,
        clear_items,
        delete_item,
        list_offers,
    )
except Exception as e:
    _db_import_error = e

try:
    from .security import verify_credentials  # keep existing login logic
except Exception as e:
    _security_import_error = e


@app.on_event("startup")
def _startup():
    if _security_import_error:
        raise RuntimeError(f"security import failed: {_security_import_error}")
    if _db_import_error:
        raise RuntimeError(f"db import failed: {_db_import_error}")
    init_db()


@app.get("/health")
def health():
    return {"ok": True}


def _require_user(request: Request):
    user = request.session.get("user")
    if not user:
        return None, RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)
    return user, None


def _get_or_create_offer_id(request: Request) -> int:
    user = request.session.get("user")
    if not user:
        raise RuntimeError("User not in session")

    offer_id = request.session.get("offer_id")
    try:
        offer_id_int = int(offer_id) if offer_id is not None else None
    except Exception:
        offer_id_int = None

    # Safety: offer must belong to the user
    if offer_id_int:
        if not get_offer(user=user, offer_id=offer_id_int):
            offer_id_int = None
            request.session.pop("offer_id", None)

    if not offer_id_int:
        offer_id_int = create_offer(user=user)
        request.session["offer_id"] = offer_id_int

    return offer_id_int


def _is_locked(offer: dict) -> bool:
    return (offer.get("status") or "DRAFT").upper() in ("SENT", "ACCEPTED")


def _normalize_status(s: str | None) -> str:
    s = (s or "").strip().upper()
    if s not in ("DRAFT", "SENT", "ACCEPTED", "REJECTED"):
        s = "DRAFT"
    return s


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user, resp = _require_user(request)
    if resp:
        return resp
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)



@app.get("/offers", response_class=HTMLResponse)
def offers_page(request: Request, status: str | None = None):
    user, resp = _require_user(request)
    if resp:
        return resp

    status_filter = (status or "").strip().upper() or None
    rows = list_offers(user, status_filter)
    # Ensure plain dicts for template safety
    offers = [dict(r) for r in rows] if rows else []

    return templates.TemplateResponse(
        "offers.html",
        {"request": request, "user": user, "offers": offers, "status_filter": status_filter},
    )


@app.post("/offers/open/{offer_id}")
def offers_open(request: Request, offer_id: int):
    user, resp = _require_user(request)
    if resp:
        return resp

    offer_row = get_offer(user=user, offer_id=int(offer_id))
    if not offer_row:
        return RedirectResponse(url="/offers", status_code=HTTP_303_SEE_OTHER)

    request.session["offer_id"] = int(offer_id)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if request.session.get("user"):
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_credentials(username, password):
        request.session["user"] = username
        request.session.pop("offer_id", None)
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Pogrešan user ili lozinka."},
    )


@app.post("/logout")
def do_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)


@app.get("/offer", response_class=HTMLResponse)
def offer_page(request: Request):
    user, resp = _require_user(request)
    if resp:
        return resp

    offer_id = _get_or_create_offer_id(request)
    offer_row = get_offer(user=user, offer_id=offer_id)
    offer = dict(offer_row) if offer_row else {}
    if _is_locked(offer):
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
    offer_row = get_offer(user=user, offer_id=offer_id)
    offer = dict(offer_row) if offer_row else {}
    clients_rows = list_clients(user)
    clients = [r["name"] for r in clients_rows] if clients_rows else []
    locked = _is_locked(offer)

    items = list_items(offer_id)
    subtotal = sum(float(i["line_total"]) for i in items) if items else 0.0

    return templates.TemplateResponse(
        "offer.html",
        {"request": request, "user": user, "offer": offer, "items": items, "subtotal": subtotal, "clients": clients, "locked": locked},
    )


@app.post("/offer/add")
def offer_add(
    request: Request,
    name: str = Form(...),
    qty: float = Form(1),
    price: float = Form(0),
):
    user, resp = _require_user(request)
    if resp:
        return resp

    offer_id = _get_or_create_offer_id(request)
    offer_row = get_offer(user=user, offer_id=offer_id)
    offer = dict(offer_row) if offer_row else {}
    if _is_locked(offer):
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

    offer_row = get_offer(user=user, offer_id=offer_id)
    offer = dict(offer_row) if offer_row else {}
    if _is_locked(offer):
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

    name = (name or "").strip()
    if name:
        add_item(offer_id=offer_id, name=name, qty=float(qty), price=float(price))
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/clear")
def offer_clear(request: Request):
    user, resp = _require_user(request)
    if resp:
        return resp

    offer_id = _get_or_create_offer_id(request)
    clear_items(offer_id)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/new")
def offer_new(request: Request):
    user, resp = _require_user(request)
    if resp:
        return resp

    request.session.pop("offer_id", None)
    offer_id = create_offer(user=user)
    request.session["offer_id"] = offer_id
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/item/delete/{item_id}")
def offer_item_delete(request: Request, item_id: int):
    user, resp = _require_user(request)
    if resp:
        return resp

    offer_id = _get_or_create_offer_id(request)
    offer_row = get_offer(user=user, offer_id=offer_id)
    offer = dict(offer_row) if offer_row else {}
    if _is_locked(offer):
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

    delete_item(offer_id=offer_id, item_id=int(item_id))
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/client")
def offer_client(request: Request, client_name: str = Form("")):
    user, resp = _require_user(request)
    if resp:
        return resp

    offer_id = _get_or_create_offer_id(request)
    offer_row = get_offer(user=user, offer_id=offer_id)
    offer = dict(offer_row) if offer_row else {}
    if _is_locked(offer):
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/status")
def offer_status(request: Request, status: str = Form("DRAFT")):
    user, resp = _require_user(request)
    if resp:
        return resp

    offer_id = _get_or_create_offer_id(request)
    status_norm = _normalize_status(status)
    update_offer_status(user=user, offer_id=offer_id, status=status_norm)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

    client_name = (client_name or "").strip() or None
    if client_name:
        upsert_client(user=user, name=client_name)
    update_offer_client_name(user=user, offer_id=offer_id, client_name=client_name)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.get("/offer.xlsx")
def offer_excel(request: Request):
    user, resp = _require_user(request)
    if resp:
        return resp

    offer_id = _get_or_create_offer_id(request)
    offer_row = get_offer(user=user, offer_id=offer_id)
    offer = dict(offer_row) if offer_row else {}

    items = list_items(offer_id)

    wb = Workbook()
    ws = wb.active
    ws.title = "Ponuda"

    ws.append(["Ponuda #", (offer.get("offer_no") or str(offer_id))])
    ws.append(["Korisnik", user])
    ws.append(["Klijent", offer.get("client_name") or ""])
    ws.append(["Datum", (offer.get("created_at").strftime("%d.%m.%Y %H:%M") if offer.get("created_at") else datetime.now().strftime("%d.%m.%Y %H:%M"))])
    ws.append([])

    ws.append(["Naziv", "Količina", "Cijena", "Ukupno"])

    subtotal = 0.0
    for it in items:
        name = it["name"]
        qty = float(it["qty"])
        price = float(it["price"])
        line_total = float(it["line_total"])
        subtotal += line_total
        ws.append([name, qty, price, line_total])

    ws.append([])
    ws.append(["", "", "Međuzbroj", subtotal])

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    filename = f"ponuda_{user}_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.xlsx"
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/offer.pdf")
def offer_pdf(request: Request):
    user, resp = _require_user(request)
    if resp:
        return resp

    offer_id = _get_or_create_offer_id(request)
    offer_row = get_offer(user=user, offer_id=offer_id)
    offer = dict(offer_row) if offer_row else {}

    items = list_items(offer_id)

    bio = BytesIO()
    c = canvas.Canvas(bio, pagesize=A4)
    width, height = A4

    y = height - 60
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, "Ponuda")

    # Logo (optional)
    try:
        if COMPANY_LOGO_PATH and os.path.exists(COMPANY_LOGO_PATH):
            c.drawImage(COMPANY_LOGO_PATH, 420, y - 6, width=120, height=40, preserveAspectRatio=True, mask="auto")
    except Exception:
        pass

    y -= 22

    c.setFont("Helvetica", 10)
    if COMPANY_NAME:
        c.drawString(50, y, COMPANY_NAME)
        y -= 14
    if COMPANY_ADDRESS:
        c.drawString(50, y, COMPANY_ADDRESS)
        y -= 14
    meta = []
    if COMPANY_OIB:
        meta.append(f"OIB: {COMPANY_OIB}")
    if COMPANY_IBAN:
        meta.append(f"IBAN: {COMPANY_IBAN}")
    if meta:
        c.drawString(50, y, "  ".join(meta))
        y -= 14
    contact = []
    if COMPANY_EMAIL:
        contact.append(COMPANY_EMAIL)
    if COMPANY_PHONE:
        contact.append(COMPANY_PHONE)
    if contact:
        c.drawString(50, y, "  ".join(contact))
        y -= 18


    c.setFont("Helvetica", 10)
    c.drawString(50, y, f"Ponuda #: {offer.get('offer_no') or str(offer_id)}")
    y -= 16
    c.drawString(50, y, f"Korisnik: {user}")
    y -= 16
    c.drawString(50, y, f"Klijent: {offer.get('client_name') or ''}")
    y -= 16
    c.drawString(50, y, f"Datum: {(offer.get('created_at').strftime('%d.%m.%Y %H:%M') if offer.get('created_at') else datetime.now().strftime('%d.%m.%Y %H:%M'))}")
    y -= 22

    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, y, "Naziv")
    c.drawString(320, y, "Kol.")
    c.drawString(380, y, "Cijena")
    c.drawString(460, y, "Ukupno")
    y -= 10
    c.line(50, y, 545, y)
    y -= 18

    c.setFont("Helvetica", 10)

    subtotal = 0.0
    for it in items:
        name = it["name"]
        qty = float(it["qty"])
        price = float(it["price"])
        line_total = float(it["line_total"])
        subtotal += line_total

        if y < 80:
            c.showPage()
            y = height - 60
            c.setFont("Helvetica-Bold", 11)
            c.drawString(50, y, "Naziv")
            c.drawString(320, y, "Kol.")
            c.drawString(380, y, "Cijena")
            c.drawString(460, y, "Ukupno")
            y -= 10
            c.line(50, y, 545, y)
            y -= 18
            c.setFont("Helvetica", 10)

        safe_name = (name[:55] + "…") if len(name) > 56 else name
        c.drawString(50, y, safe_name)
        c.drawRightString(360, y, f"{qty:.2f}")
        c.drawRightString(440, y, f"{price:.2f}")
        c.drawRightString(545, y, f"{line_total:.2f}")
        y -= 16

    y -= 8
    c.line(350, y, 545, y)
    y -= 18
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(440, y, "Međuzbroj:")
    c.drawRightString(545, y, f"{subtotal:.2f} €")

    c.showPage()
    c.save()
    bio.seek(0)

    filename = f"ponuda_{user}_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.pdf"
    return StreamingResponse(
        bio,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
