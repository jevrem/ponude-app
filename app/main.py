import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from .security import verify_credentials, require_login, logout

# --- App setup ---
app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret-change-me")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=False,  # na Renderu je HTTPS; kasnije možemo prebaciti na True
)

# --- Helpers for offer items (stored in session for now) ---
def _get_items(request: Request) -> list[dict]:
    return request.session.get("offer_items", [])

def _set_items(request: Request, items: list[dict]) -> None:
    request.session["offer_items"] = items

def _calc_total(items: list[dict]) -> float:
    return float(sum(float(i.get("total", 0) or 0) for i in items))


# --- Basic routes ---
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = request.session.get("user")
    if not user:
        return RedirectResponse(url="/login", status_code=HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("app.html", {"request": request, "user": user})


# --- Auth ---
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if request.session.get("user"):
        return RedirectResponse(url="/", status_code=HTTP_303_SEE_OTHER)
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


# --- Offer MVP (session-based) ---
@app.get("/offer", response_class=HTMLResponse)
def offer_page(request: Request):
    require_login(request)
    items = _get_items(request)
    subtotal = _calc_total(items)
    return templates.TemplateResponse(
        "offer.html",
        {
            "request": request,
            "user": request.session.get("user"),
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

    qty = float(qty or 0)
    price = float(price or 0)
    total = qty * price

    items = _get_items(request)
    items.append(
        {
            "name": name.strip(),
            "qty": qty,
            "price": price,
            "total": total,
        }
    )
    _set_items(request, items)

    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

@app.post("/offer/clear")
def offer_clear(request: Request):
    require_login(request)
    _set_items(request, [])
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
