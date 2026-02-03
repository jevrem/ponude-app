import os

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from .security import verify_credentials, require_login, logout

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

# Security: obavezno postavi SECRET_KEY u Render Environment Variables
SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret-change-me")

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    https_only=True,  # Render + custom domain = HTTPS
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
    return templates.TemplateResponse(
        "offer.html",
        {
            "request": request,
            "user": request.session.get("user"),
            "items" = request.session.get("offer_items", [])
return templates.TemplateResponse(
    "offer.html",
    {"request": request, "user": request.session.get("user"), "items": items},
)
        },
    )
@app.post("/offer/add")
def offer_add(
    request: Request,
    name: str = Form(...),
    qty: float = Form(...),
    price: float = Form(...),
):
    require_login(request)

    items = request.session.get("offer_items", [])
    total = float(qty) * float(price)

    items.append(
        {"name": name.strip(), "qty": float(qty), "price": float(price), "total": total}
    )
    request.session["offer_items"] = items

    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/delete")
def offer_delete(request: Request, idx: int = Form(...)):
    require_login(request)

    items = request.session.get("offer_items", [])
    if 0 <= idx < len(items):
        items.pop(idx)
    request.session["offer_items"] = items

    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)
