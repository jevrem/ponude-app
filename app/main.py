import os
from typing import List, Dict

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from .security import verify_credentials, require_login, logout

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret-change-me")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=False,  # kad sve radi i domena je OK, možemo prebaciti na True
)


def _get_offer_items(request: Request) -> List[Dict]:
    items = request.session.get("offer_items")
    if not isinstance(items, list):
        items = []
        request.session["offer_items"] = items
    return items


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

    items = _get_offer_items(request)

    subtotal = sum(float(i.get("total", 0)) for i in items)
    return templates.TemplateResponse(
        "offer.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "items": items,
            "subtotal": f"{subtotal:.2f}",
        },
    )


@app.post("/offer/add")
@app.post("/offer/add/")
def offer_add(
    request: Request,
    name: str = Form(""),
    qty: float = Form(1),
    price: float = Form(0),
):
    require_login(request)

    name = (name or "").strip()
    if not name:
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

    try:
        qty_f = float(qty)
    except Exception:
        qty_f = 1.0

    try:
        price_f = float(price)
    except Exception:
        price_f = 0.0

    total = qty_f * price_f

    items = request.session.get("offer_items") or []
    if not isinstance(items, list):
        items = []

    items.append({"name": name, "qty": qty_f, "price": price_f, "total": total})
    request.session["offer_items"] = items

    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)



@app.post("/offer/clear")
def offer_clear(request: Request):
    require_login(request)
    request.session["offer_items"] = []
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
