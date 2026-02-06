import os
from datetime import datetime
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from app.security import require_login, verify_credentials, logout
from app.db import (
    init_db,
    create_offer,
    get_offer,
    update_offer_client_name,
    update_offer_status,
    update_offer_meta,
    accept_offer,
    add_item,
    list_items,
    delete_item,
    clear_items,
    list_offers,
    get_settings,
    upsert_settings,
)

BASE_DIR = os.path.dirname(__file__)
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

app = FastAPI()

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)


@app.on_event("startup")
def _startup() -> None:
    init_db()


def _current_offer_id(request: Request, user: str) -> int:
    offer_id = request.session.get("offer_id")
    if isinstance(offer_id, int) and offer_id > 0:
        # Ensure it belongs to user; if not, create a fresh one.
        o = get_offer(offer_id, user)
        if o:
            return offer_id

    offer_id = create_offer(user)
    request.session["offer_id"] = int(offer_id)
    return int(offer_id)


def _offer_context(request: Request, user: str) -> dict:
    offer_id = _current_offer_id(request, user)
    offer = get_offer(offer_id, user) or {}
    items = list_items(offer_id)
    subtotal = sum(float(i.get("line_total") or 0) for i in items) if items else 0.0
    vat_rate = float(offer.get("vat_rate") or 0)
    vat_amount = subtotal * (vat_rate / 100.0)
    total = subtotal + vat_amount
    settings = get_settings(user) or {}  # dict
    locked = (offer.get("status") == "ACCEPTED")  # ONLY ACCEPTED locks edits

    return {
        "offer_id": offer_id,
        "offer": offer,
        "items": items,
        "subtotal": subtotal,
        "vat_rate": vat_rate,
        "vat_amount": vat_amount,
        "total": total,
        "settings": settings,
        "locked": locked,
    }


@app.get("/", response_class=HTMLResponse)
def root(_: Request):
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def do_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    username = (username or "").strip()
    password = (password or "").strip()

    if verify_credentials(username, password):
        request.session.clear()
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
    user = require_login(request)
    ctx = _offer_context(request, user)
    return templates.TemplateResponse(
        "offer.html",
        {"request": request, "user": user, "meta_saved": request.query_params.get("meta_saved")=="1", **ctx},
    )


@app.post("/offer/new")
def offer_new(request: Request):
    user = require_login(request)
    offer_id = create_offer(user)
    request.session["offer_id"] = int(offer_id)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/select")
def offer_select(request: Request, offer_id: int = Form(...)):
    user = require_login(request)
    # only allow selecting own offers
    if get_offer(int(offer_id), user):
        request.session["offer_id"] = int(offer_id)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/client")
def offer_client(request: Request, client_name: str = Form("")):
    user = require_login(request)
    ctx = _offer_context(request, user)
    if ctx["locked"]:
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

    update_offer_client_name(ctx["offer_id"], user, (client_name or "").strip())
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/status")
def offer_status(request: Request, status: str = Form("DRAFT")):
    user = require_login(request)
    ctx = _offer_context(request, user)
    # Only ACCEPTED is final; don't auto-lock on SENT.
    status = (status or "DRAFT").strip().upper()
    if status not in ("DRAFT", "SENT", "ACCEPTED", "REJECTED"):
        status = "DRAFT"

    # If already accepted, ignore.
    if (ctx["offer"].get("status") == "ACCEPTED"):
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

    update_offer_status(ctx["offer_id"], user, status)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/accept")
def offer_accept(request: Request):
    user = require_login(request)
    ctx = _offer_context(request, user)
    # "Prihvati ponudu" sets ACCEPTED and locks it.
    accept_offer(ctx["offer_id"], user)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/meta")
def offer_meta(
    request: Request,
    client_name: str = Form(""),
    place: str = Form(""),
    signed_by: str = Form(""),
    delivery_term: str = Form(""),
    payment_term: str = Form(""),
    note: str = Form(""),
    vat_rate: int = Form(0),
):
    """
    Spremanje detalja (koji idu u PDF/Excel) za aktivnu ponudu.
    Status se mijenja samo preko /offer/status ili /offer/accept.
    """
    user = require_login(request)
    offer_id = request.session.get("offer_id")
    if not offer_id:
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

    update_offer_meta(
        int(offer_id),
        user,
        (client_name or "").strip() or None,
        (place or "").strip() or None,
        (signed_by or "").strip() or None,
        (delivery_term or "").strip() or None,
        (payment_term or "").strip() or None,
        (note or "").strip() or None,
        int(vat_rate or 0),
    )
    return RedirectResponse(url="/offer?meta_saved=1", status_code=HTTP_303_SEE_OTHER)
@app.get("/offer/meta")
def offer_meta_get(request: Request):
    user = require_login(request)
    ctx = _offer_context(request, user)
    o = ctx["offer"] or {}
    return JSONResponse(
        {
            "offer_id": ctx["offer_id"],
            "offer_no": o.get("offer_no"),
            "status": o.get("status"),
            "client_name": o.get("client_name"),
            "place": o.get("place"),
            "sign_name": o.get("sign_name"),
            "delivery_term": o.get("delivery_term"),
            "payment_term": o.get("payment_term"),
            "note": o.get("note"),
            "vat_rate": o.get("vat_rate"),
        }
    )


@app.post("/offer/add")
def offer_add(
    request: Request,
    name: str = Form(...),
    qty: float = Form(1),
    price: float = Form(0),
):
    user = require_login(request)
    ctx = _offer_context(request, user)
    if ctx["locked"]:
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

    name = (name or "").strip()
    if name:
        add_item(ctx["offer_id"], name=name, qty=float(qty or 0), price=float(price or 0))
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/delete")
def offer_delete(
    request: Request,
    item_id: int = Form(...),
):
    user = require_login(request)
    ctx = _offer_context(request, user)
    if ctx["locked"]:
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

    delete_item(ctx["offer_id"], int(item_id))
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.post("/offer/clear")
def offer_clear(request: Request):
    user = require_login(request)
    ctx = _offer_context(request, user)
    if ctx["locked"]:
        return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)

    clear_items(ctx["offer_id"])
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.get("/offers", response_class=HTMLResponse)
def offers_page(request: Request, status: str | None = None):
    user = require_login(request)
    status_filter = (status or "").strip().upper() or None
    if status_filter not in (None, "DRAFT", "SENT", "ACCEPTED", "REJECTED"):
        status_filter = None

    offers = list_offers(user, status_filter)
    return templates.TemplateResponse(
        "offers.html",
        {"request": request, "user": user, "offers": offers, "status_filter": status_filter},
    )


@app.post("/offers/open")
def offers_open(request: Request, offer_id: int = Form(...)):
    user = require_login(request)
    if get_offer(int(offer_id), user):
        request.session["offer_id"] = int(offer_id)
    return RedirectResponse(url="/offer", status_code=HTTP_303_SEE_OTHER)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    user = require_login(request)
    s = get_settings(user) or {}
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "s": s, "ok": None})


@app.post("/settings")
def settings_save(
    request: Request,
    company_name: str = Form(""),
    company_address: str = Form(""),
    company_oib: str = Form(""),
    company_phone: str = Form(""),
    company_email: str = Form(""),
):
    user = require_login(request)
    upsert_settings(
        user=user,
        company_name=(company_name or "").strip(),
        company_address=(company_address or "").strip(),
        company_oib=(company_oib or "").strip(),
        company_phone=(company_phone or "").strip(),
        company_email=(company_email or "").strip(),
    )
    s = get_settings(user) or {}
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "s": s, "ok": True})


@app.get("/__routes")
def __routes():
    # Helps debugging on Render when you think a route is missing.
    return JSONResponse(sorted([f"{r.path} [{','.join(r.methods or [])}]" for r in app.router.routes]))