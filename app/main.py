import os
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from .security import verify_credentials, require_login, logout
from .db import get_conn

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret-change-me")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=False,  # možeš prebaciti na True kad ti je domena 100% OK
)

def _session_items(request: Request) -> list[dict]:
    items = request.session.get("offer_items")
    if not isinstance(items, list):
        items = []
        request.session["offer_items"] = items
    return items

def _to_decimal(x: str, default: Decimal) -> Decimal:
    try:
        # podrži i "1,23"
        x = (x or "").strip().replace(",", ".")
        if x == "":
            return default
        return Decimal(x)
    except (InvalidOperation, ValueError):
        return default

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
    items = _session_items(request)

    subtotal = sum(Decimal(str(i["line_total"])) for i in items) if items else Decimal("0")
    return templates.TemplateResponse(
        "offer.html",
        {"request": request, "items": items, "subtotal": subtotal},
    )

@app.post("/offer/add")
def offer_add(
    request: Request,
    name: str = Form(...),
    qty: str = Form("1"),
    price: str = Form("0"),
):
    require_login(request)

    name = (name or "").strip()
    if not name:
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

    qty_d = _to_decimal(qty, Decimal("1"))
    price_d = _to_decimal(price, Decimal("0"))
    line_total = (qty_d * price_d).quantize(Decimal("0.01"))

    # 1) session (kao MVP)
    items = _session_items(request)
    items.append(
        {
            "name": name,
            "qty": float(qty_d),
            "price": float(price_d),
            "line_total": float(line_total),
        }
    )
    request.session["offer_items"] = items

    # 2) DB (Postgres)
    user = request.session.get("user")

    with get_conn() as conn:
        with conn.cursor() as cur:
            # za MVP: jedna "aktivna ponuda" po sessionu
            offer_id = request.session.get("offer_id")

            if not offer_id:
                cur.execute(
                    "insert into offers (user_name, client_name) values (%s, %s) returning id",
                    (user, None),
                )
                offer_id = cur.fetchone()["id"]
                request.session["offer_id"] = offer_id

            cur.execute(
                """
                insert into offer_items (offer_id, name, qty, price, line_total)
                values (%s, %s, %s, %s, %s)
                """,
                (offer_id, name, qty_d, price_d, line_total),
            )

            conn.commit()

    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

@app.post("/offer/clear")
def offer_clear(request: Request):
    require_login(request)

    request.session["offer_items"] = []
    request.session.pop("offer_id", None)

    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

@app.get("/app", response_class=HTMLResponse)
def app_page(request: Request):
    require_login(request)
    return templates.TemplateResponse(
        "app.html",
        {"request": request, "user": request.session.get("user")},
    )
