import os

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from .security import verify_credentials, require_login, logout
from .db import create_offer, list_items, add_item, clear_items

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret-change-me")

# Render često ima env var RENDER="true". Lokalno ga nema.
HTTPS_ONLY = True if os.getenv("RENDER") else False

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=HTTPS_ONLY,
)

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
def do_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_credentials(username, password):
        request.session["user"] = username
        # reset aktivne ponude pri loginu (da ne vuče staru)
        request.session.pop("offer_id", None)
        return RedirectResponse(url="/", status_code=HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Pogrešan user ili lozinka."})

@app.post("/logout")
def do_logout(request: Request):
    logout(request)
    request.session.pop("offer_id", None)
    return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)

@app.get("/offer", response_class=HTMLResponse)
def offer_page(request: Request):
    require_login(request)
    user = request.session["user"]

    # Držimo "aktivnu ponudu" u sessionu
    offer_id = request.session.get("offer_id")
    if not offer_id:
        offer_id = create_offer(user_name=user, client_name=None)
        request.session["offer_id"] = offer_id

    items = list_items(offer_id)
    subtotal = sum(float(i["line_total"]) for i in items) if items else 0.0

    return templates.TemplateResponse(
        "offer.html",
        {
            "request": request,
            "user": user,
            "offer_id": offer_id,
            "items": items,
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
    user = request.session["user"]

    offer_id = request.session.get("offer_id")
    if not offer_id:
        offer_id = create_offer(user_name=user, client_name=None)
        request.session["offer_id"] = offer_id

    name = (name or "").strip()
    if name:
        add_item(offer_id=offer_id, name=name, qty=float(qty), price=float(price))

    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

@app.post("/offer/clear")
def offer_clear(request: Request):
    require_login(request)
    offer_id = request.session.get("offer_id")
    if offer_id:
        clear_items(offer_id)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
