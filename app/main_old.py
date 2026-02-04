import os
from pathlib import Path
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
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from .db import (
    init_db,
    create_offer,
    get_offer,
    update_offer_client_name,
    update_offer_status,
    list_items,
    add_item,
    clear_items,
    delete_item,
    list_offers,
    list_clients,
    upsert_client,
    upsert_settings,
    get_settings,
    update_offer_meta,    accept_offer,
)
from .security import verify_credentials


BASE_DIR = Path(__file__).resolve().parent

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret-change-me")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)

COMPANY_NAME = os.getenv("COMPANY_NAME", "Amaryllis interijeri")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "")
COMPANY_OIB = os.getenv("COMPANY_OIB", "")
COMPANY_IBAN = os.getenv("COMPANY_IBAN", "")
COMPANY_EMAIL = os.getenv("COMPANY_EMAIL", "")
COMPANY_PHONE = os.getenv("COMPANY_PHONE", "")
COMPANY_LOGO_PATH = os.getenv("COMPANY_LOGO_PATH", str(BASE_DIR / "static" / "logo.png"))

PDF_FONT_PATH = os.getenv("PDF_FONT_PATH", str(BASE_DIR / "static" / "DejaVuSans.ttf"))
PDF_FONT_NAME = os.getenv("PDF_FONT_NAME", "DejaVuSans")

_pdf_font_ready = False


def _ensure_pdf_font():
    global _pdf_font_ready
    if _pdf_font_ready:
        return
    try:
        if PDF_FONT_PATH and os.path.exists(PDF_FONT_PATH):
            pdfmetrics.registerFont(TTFont(PDF_FONT_NAME, PDF_FONT_PATH))
            _pdf_font_ready = True
    except Exception:
        _pdf_font_ready = False


from reportlab.pdfgen import canvas as _rl_canvas


class _PageNumCanvas(_rl_canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_page_number(num_pages)
            super().showPage()
        super().save()

    def _draw_page_number(self, page_count: int):
        self.setFont("Helvetica", 9)
        self.drawRightString(545, 20, f"Str. {self._pageNumber}/{page_count}")

@app.on_event("startup")
def _startup():
    init_db()


def _require_user(request: Request):
    user = request.session.get("user")
    if not user:
        return None, RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)
    return user, None

def _normalize_status(s: str | None) -> str:
    s = (s or "").strip().upper()
    if s not in ("DRAFT", "SENT", "ACCEPTED", "REJECTED"):
        s = "DRAFT"
    return s


def _is_locked(offer: dict) -> bool:
    return (offer.get("status") == "ACCEPTED")


def _get_or_create_offer_id(request: Request) -> int:
    user = request.session.get("user")
    if not user:
        raise RuntimeError("User not in session")

    offer_id = request.session.get("offer_id")
    try:
        offer_id_int = int(offer_id) if offer_id is not None else None
    except Exception:
        offer_id_int = None

    if offer_id_int:
        if not get_offer(user=user, offer_id=offer_id_int):
            offer_id_int = None
            request.session.pop("offer_id", None)

    if not offer_id_int:
        offer_id_int = create_offer(user=user)
        request.session["offer_id"] = offer_id_int

    return offer_id_int


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user, resp = _require_user(request)
    if resp:
        return resp
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
    return templates.TemplateResponse("login.html", {"request": request, "error": "Pogrešan user ili lozinka."})


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
    locked = _is_locked(offer)

    clients_rows = list_clients(user)
    clients = [r["name"] for r in clients_rows] if clients_rows else []

    items = list_items(offer_id)
    subtotal = sum(float(i["line_total"]) for i in items) if items else 0.0

    return templates.TemplateResponse(
        "offer.html",
        {"request": request, "user": user, "offer": offer, "locked": locked, "clients": clients, "items": items, "subtotal": subtotal},
    )


@app.post("/offer/add")
def offer_add(request: Request, name: str = Form(...), qty: float = Form(1), price: float = Form(0)):
    user, resp = _require_user(request)
    if resp:
        return resp

    offer_id = _get_or_create_offer_id(request)
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
    offer_row = get_offer(user=user, offer_id=offer_id)
    offer = dict(offer_row) if offer_row else {}
    if _is_locked(offer):
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

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

    client_name = (client_name or "").strip() or None
    if client_name:
        upsert_client(user=user, name=client_name)
    update_offer_client_name(user=user, offer_id=offer_id, client_name=client_name)
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


@app.post("/offer/meta")
def offer_meta(
    request: Request,
    terms_delivery: str = Form(""),
    terms_payment: str = Form(""),
    note: str = Form(""),
    place: str = Form(""),
    signed_by: str = Form(""),
    vat_rate: float = Form(0),
):
    user, resp = _require_user(request)
    if resp:
        return resp

    offer_id = _get_or_create_offer_id(request)

    offer_row = get_offer(user=user, offer_id=offer_id)
    offer = dict(offer_row) if offer_row else {}
    if _is_locked(offer):
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

    update_offer_meta(
        user=user,
        offer_id=offer_id,
        terms_delivery=(terms_delivery or "").strip() or None,
        terms_payment=(terms_payment or "").strip() or None,
        note=(note or "").strip() or None,
        place=(place or "").strip() or None,
        signed_by=(signed_by or "").strip() or None,
        vat_rate=float(vat_rate or 0),
    )
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
@app.post("/offer/accept")
def offer_accept(request: Request):
    user, resp = _require_user(request)
    if resp:
        return resp

    offer_id = _get_or_create_offer_id(request)
    accept_offer(user=user, offer_id=offer_id)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


    update_offer_meta(
        user=user,
        offer_id=offer_id,
        terms_delivery=(terms_delivery or "").strip() or None,
        terms_payment=(terms_payment or "").strip() or None,
        note=(note or "").strip() or None,
        place=(place or "").strip() or None,
        signed_by=(signed_by or "").strip() or None,
        vat_rate=float(vat_rate or 0),
    )
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.get("/offers", response_class=HTMLResponse)
def offers_page(request: Request, status: str | None = None):
    user, resp = _require_user(request)
    if resp:
        return resp

    status_filter = (status or "").strip().upper() or None
    rows = list_offers(user, status_filter)
    offers = [dict(r) for r in rows] if rows else []

    return templates.TemplateResponse("offers.html", {"request": request, "user": user, "offers": offers, "status_filter": status_filter})


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


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    user, resp = _require_user(request)
    if resp:
        return resp

    row = get_settings(user)
    settings = dict(row) if row else {}
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "settings": settings})


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
    user, resp = _require_user(request)
    if resp:
        return resp

    upsert_settings(
        user=user,
        company_name=(company_name or "").strip() or None,
        company_address=(company_address or "").strip() or None,
        company_oib=(company_oib or "").strip() or None,
        company_iban=(company_iban or "").strip() or None,
        company_email=(company_email or "").strip() or None,
        company_phone=(company_phone or "").strip() or None,
        logo_path=(logo_path or "").strip() or None,
    )
    return RedirectResponse(url="/settings", status_code=HTTP_303_SEE_OTHER)


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

    ws.append(["Ponuda #", offer.get("offer_no") or str(offer_id)])
    ws.append(["Korisnik", user])
    ws.append(["Klijent", offer.get("client_name") or ""])
    ws.append(["Datum", (offer.get("created_at").strftime("%d.%m.%Y %H:%M") if offer.get("created_at") else datetime.now().strftime("%d.%m.%Y %H:%M"))])
    ws.append(["Status", offer.get("status") or "DRAFT"])
    ws.append([])

    ws.append(["Naziv", "Količina", "Cijena", "Ukupno"])

    subtotal = 0.0
    for it in items:
        qty = float(it["qty"])
        price = float(it["price"])
        line_total = float(it["line_total"])
        subtotal += line_total
        ws.append([it["name"], qty, price, line_total])

    ws.append([])
    vat_rate = float(offer.get("vat_rate") or 0)
    vat_amount = subtotal * (vat_rate / 100.0)
    gross_total = subtotal + vat_amount

    ws.append(["", "", "Međuzbroj", subtotal])
    ws.append(["", "", f"PDV ({vat_rate:.0f}%)", vat_amount])
    ws.append(["", "", "Ukupno s PDV-om", gross_total])

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
    # auto_set_sent_on_pdf
    user, resp = _require_user(request)
    if resp:
        return resp

    offer_id = _get_or_create_offer_id(request)
    offer_row = get_offer(user=user, offer_id=offer_id)
    offer = dict(offer_row) if offer_row else {}
    items = list_items(offer_id)
    _ensure_pdf_font()
    title_font = PDF_FONT_NAME if _pdf_font_ready else "Helvetica"
    body_font = PDF_FONT_NAME if _pdf_font_ready else "Helvetica"

    bio = BytesIO()
    c = _PageNumCanvas(bio, pagesize=A4)
    width, height = A4

    y = height - 60
    c.setFont(title_font, 16)
    c.drawString(50, y, "Ponuda")

    y -= 24
    c.setFont(body_font, 10)

    row_settings = get_settings(user)
    settings = dict(row_settings) if row_settings else {}

    # Prefer DB settings over ENV
    company_name = settings.get('company_name') or COMPANY_NAME
    company_address = settings.get('company_address') or COMPANY_ADDRESS
    company_oib = settings.get('company_oib') or COMPANY_OIB
    company_iban = settings.get('company_iban') or COMPANY_IBAN
    company_email = settings.get('company_email') or COMPANY_EMAIL
    company_phone = settings.get('company_phone') or COMPANY_PHONE
    logo_path = settings.get('logo_path') or COMPANY_LOGO_PATH

    # Logo (optional)
    try:
        if logo_path and os.path.exists(logo_path):
            c.drawImage(logo_path, 420, y + 10, width=120, height=40, preserveAspectRatio=True, mask="auto")
    except Exception:
        pass


    # Company block (only if set)
    if company_name:
        c.drawString(50, y, company_name); y -= 14
    if company_address:
        c.drawString(50, y, company_address); y -= 14
    company_line = " • ".join([p for p in [f"OIB: {company_oib}" if company_oib else "", f"IBAN: {company_iban}" if company_iban else ""] if p])
    if company_line:
        c.drawString(50, y, company_line); y -= 14
    contact_line = " • ".join([p for p in [company_email, company_phone] if p])
    if contact_line:
        c.drawString(50, y, contact_line); y -= 14

    y -= 6
    c.line(50, y, 545, y)
    y -= 18

    c.setFont(body_font, 10)
    c.drawString(50, y, f"Ponuda #: {offer.get('offer_no') or str(offer_id)}"); y -= 14
    c.drawString(50, y, f"Korisnik: {user}"); y -= 14
    c.drawString(50, y, f"Klijent: {offer.get('client_name') or ''}"); y -= 14
    c.drawString(50, y, f"Status: {offer.get('status') or 'DRAFT'}"); y -= 14
    if offer.get("accepted_at"):
        try:
            c.drawString(50, y, f"Prihvaćeno: {offer.get('accepted_at').strftime('%d.%m.%Y %H:%M')}"); y -= 14
        except Exception:
            pass
    c.drawString(50, y, f"Mjesto: {offer.get('place') or '' }"); y -= 14
    c.drawString(50, y, f"Rok isporuke: {offer.get('terms_delivery') or '' }"); y -= 14
    c.drawString(50, y, f"Rok plaćanja: {offer.get('terms_payment') or '' }"); y -= 14
    c.drawString(50, y, f"Napomena: {offer.get('note') or '' }"); y -= 14

    c.drawString(50, y, f"Datum: {(offer.get('created_at').strftime('%d.%m.%Y %H:%M') if offer.get('created_at') else datetime.now().strftime('%d.%m.%Y %H:%M'))}")
    y -= 22

    c.setFont(body_font, 11)
    c.drawString(50, y, "Naziv")
    c.drawString(320, y, "Kol.")
    c.drawString(380, y, "Cijena")
    c.drawString(460, y, "Ukupno")
    y -= 10
    c.line(50, y, 545, y)
    y -= 18

    c.setFont(body_font, 10)
    subtotal = 0.0
    for it in items:
        name = it["name"]
        qty = float(it["qty"])
        price = float(it["price"])
        line_total = float(it["line_total"])
        subtotal += line_total

        if y < 90:
            # footer on page break
            try:
                c.setFont(body_font, 9)
                footer = " • ".join([p for p in [f"OIB: {company_oib}" if company_oib else "", company_email or "", company_phone or ""] if p])
                if footer:
                    c.drawString(50, 40, footer)
            except Exception:
                pass

            c.showPage()
            y = height - 60
            c.setFont(body_font, 11)
            c.drawString(50, y, "Naziv")
            c.drawString(320, y, "Kol.")
            c.drawString(380, y, "Cijena")
            c.drawString(460, y, "Ukupno")
            y -= 10
            c.line(50, y, 545, y)
            y -= 18
            c.setFont(body_font, 10)

        safe_name = (name[:55] + "…") if len(name) > 56 else name
        c.drawString(50, y, safe_name)
        c.drawRightString(360, y, f"{qty:.2f}")
        c.drawRightString(440, y, f"{price:.2f}")
        c.drawRightString(545, y, f"{line_total:.2f}")
        y -= 16

    y -= 8
    c.line(350, y, 545, y)
    y -= 18
    c.setFont(body_font, 12)
    vat_rate = float(offer.get("vat_rate") or 0)
    vat_amount = subtotal * (vat_rate / 100.0)
    gross_total = subtotal + vat_amount

    c.drawRightString(440, y, "Međuzbroj:")
    c.drawRightString(545, y, f"{subtotal:.2f} €")
    y -= 16
    c.setFont(body_font, 10)
    c.drawRightString(440, y, f"PDV ({vat_rate:.0f}%):")
    c.drawRightString(545, y, f"{vat_amount:.2f} €")
    y -= 16
    c.setFont(body_font, 12)
    c.drawRightString(440, y, "Ukupno:")
    c.drawRightString(545, y, f"{gross_total:.2f} €")

    y -= 50
    c.setFont(body_font, 10)
    c.drawString(50, y, "Potpis: ____________________________")
    y -= 14
    c.drawString(50, y, f"{offer.get('signed_by') or '' }")

    # Footer
    try:
        c.setFont(body_font, 9)
        footer = " • ".join([p for p in [f"OIB: {company_oib}" if company_oib else "", f"IBAN: {company_iban}" if company_iban else "", company_email or "", company_phone or ""] if p])
        if footer:
            c.drawString(50, 40, footer)
    except Exception:
        pass

    c.showPage()
    c.save()
    bio.seek(0)

    filename = f"ponuda_{user}_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.pdf"
    return StreamingResponse(bio, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
