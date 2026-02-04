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
    https_only=False,
)

_db_import_error = None
_security_import_error = None

try:
    from .db import init_db, create_offer, get_offer, update_offer_client_name, list_items, add_item, clear_items, delete_item
except Exception as e:
    _db_import_error = e

try:
    from .security import verify_credentials, require_login, logout
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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = request.session.get("user")
    if not user:
        return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)
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
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Pogrešan user ili lozinka."},
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
    subtotal = sum(float(i["line_total"]) for i in items) if items else 0.0
    return templates.TemplateResponse(
        "offer.html",
        {"request": request, "user": user, "items": items, "subtotal": subtotal},
    )


@app.post("/offer/add")
def offer_add(
    request: Request,
    name: str = Form(...),
    qty: float = Form(1),
    price: float = Form(0),
):
    require_login(request)
    user = request.session.get("user")
    name = (name or "").strip()
    if name:
        add_item(user=user, name=name, qty=float(qty), price=float(price))
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/clear")
def offer_clear(request: Request):
    require_login(request)
    user = request.session.get("user")
    clear_items(user)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/item/delete/{item_id}")
def offer_item_delete(request: Request, item_id: int):
    require_login(request)
    offer_id = _get_or_create_offer_id(request)
    delete_item(offer_id=offer_id, item_id=int(item_id))
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.get("/offer.xlsx")
def offer_excel(request: Request):
    require_login(request)
    user = request.session.get("user")
    items = list_items(user)

    wb = Workbook()
    ws = wb.active
    ws.title = "Ponuda"

    ws.append(["Ponuda #", offer_id])
    ws.append(["Korisnik", user])
    ws.append(["Klijent", (offer.get("client_name") or "")])
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
    require_login(request)
    user = request.session.get("user")
    items = list_items(user)

    bio = BytesIO()
    c = canvas.Canvas(bio, pagesize=A4)
    width, height = A4

    y = height - 60
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, "Ponuda")
    y -= 22

    c.setFont("Helvetica", 10)
    # offer already loaded as dict above

    c.drawString(50, y, f"Ponuda #: {offer_id}")
    y -= 16
    c.drawString(50, y, f"Korisnik: {user}")
    y -= 16
    c.drawString(50, y, f"Klijent: {(offer["client_name"] if offer else None) or ""}")
    y -= 16
    c.drawString(50, y, f"Datum: {(offer.get("created_at").strftime("%d.%m.%Y %H:%M") if offer.get("created_at") else datetime.now().strftime("%d.%m.%Y %H:%M"))}")
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


@app.post("/offer/client")
def offer_client(request: Request, client_name: str = Form("")):
    require_login(request)
    user = request.session.get("user")
    offer_id = _get_or_create_offer_id(request)
    client_name = (client_name or "").strip() or None
    update_offer_client_name(user=user, offer_id=offer_id, client_name=client_name)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
