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

from .security import verify_credentials, require_login, logout
from .db import init_db, list_items, add_item, clear_items


app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret-change-me")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=False,  # možeš staviti True kad želiš strict cookie samo na HTTPS
)

# init DB (sqlite create tables, etc.)
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
    # items: list dict/rows: {name, qty, price, line_total}
    subtotal = 0.0
    norm_items = []
    for it in items:
        # podrži dict ili sqlite.Row
        name = it["name"] if hasattr(it, "__getitem__") else it.get("name", "")
        qty = float(it["qty"] if hasattr(it, "__getitem__") else it.get("qty", 0))
        price = float(it["price"] if hasattr(it, "__getitem__") else it.get("price", 0))

        # podrži line_total ili total
        if hasattr(it, "__getitem__"):
            lt = it["line_total"] if "line_total" in it.keys() else it.get("total", 0)
        else:
            lt = it.get("line_total", it.get("total", 0))
        line_total = float(lt)

        subtotal += line_total
        norm_items.append(
            {"name": name, "qty": qty, "price": price, "line_total": line_total}
        )

    return templates.TemplateResponse(
        "offer.html",
        {
            "request": request,
            "user": user,
            "items": norm_items,
            "subtotal": subtotal,
        },
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
        add_item(user=user, name=name, qty=qty, price=price)

    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/clear")
def offer_clear(request: Request):
    require_login(request)
    user = request.session.get("user")
    clear_items(user)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.get("/offer.xlsx")
def offer_excel(request: Request):
    require_login(request)
    user = request.session.get("user")
    items = list_items(user)

    wb = Workbook()
    ws = wb.active
    ws.title = "Ponuda"

    ws.append(["Naziv", "Količina", "Cijena", "Ukupno"])

    subtotal = 0.0
    for it in items:
        name = it["name"] if hasattr(it, "__getitem__") else it.get("name", "")
        qty = float(it["qty"] if hasattr(it, "__getitem__") else it.get("qty", 0))
        price = float(it["price"] if hasattr(it, "__getitem__") else it.get("price", 0))

        if hasattr(it, "__getitem__"):
            lt = it["line_total"] if "line_total" in it.keys() else it.get("total", 0)
        else:
            lt = it.get("line_total", it.get("total", 0))
        line_total = float(lt)

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
    c.drawString(50, y, f"Korisnik: {user}")
    y -= 16
    c.drawString(50, y, f"Datum: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
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
        name = it["name"] if hasattr(it, "__getitem__") else it.get("name", "")
        qty = float(it["qty"] if hasattr(it, "__getitem__") else it.get("qty", 0))
        price = float(it["price"] if hasattr(it, "__getitem__") else it.get("price", 0))

        if hasattr(it, "__getitem__"):
            lt = it["line_total"] if "line_total" in it.keys() else it.get("total", 0)
        else:
            lt = it.get("line_total", it.get("total", 0))
        line_total = float(lt)

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
