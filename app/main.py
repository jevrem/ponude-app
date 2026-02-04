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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user, resp = _require_user(request)
    if resp:
        return resp
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)



@app.get("/offers", response_class=HTMLResponse)
def offers_page(request: Request):
    user, resp = _require_user(request)
    if resp:
        return resp

    rows = list_offers(user)
    # Ensure plain dicts for template safety
    offers = [dict(r) for r in rows] if rows else []

    return templates.TemplateResponse(
        "offers.html",
        {"request": request, "user": user, "offers": offers},
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

    items = list_items(offer_id)
    subtotal = sum(float(i["line_total"]) for i in items) if items else 0.0

    return templates.TemplateResponse(
        "offer.html",
        {"request": request, "user": user, "offer": offer, "items": items, "subtotal": subtotal},
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
    delete_item(offer_id=offer_id, item_id=int(item_id))
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/client")
def offer_client(request: Request, client_name: str = Form("")):
    user, resp = _require_user(request)
    if resp:
        return resp

    offer_id = _get_or_create_offer_id(request)
    client_name = (client_name or "").strip() or None
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
    y -= 22

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
