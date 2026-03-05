import os
import io
from datetime import datetime
from typing import List, Dict, Any

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import get_conn
from .security import new_portal_token
from .pdf import build_offer_pdf

app = FastAPI()

BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

def audit(conn, action: str, entity_type: str = None, entity_id: int = None, meta: Dict[str, Any] = None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log(action, entity_type, entity_id, meta) VALUES (%s,%s,%s,%s)",
            (action, entity_type, entity_id, meta),
        )

def ensure_schema():
    # Optional: auto-create schema if you want; default is off.
    return

def next_offer_number(conn, year: int) -> (int, str):
    with conn.cursor() as cur:
        cur.execute("SELECT last_number FROM offer_counters WHERE year=%s FOR UPDATE", (year,))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO offer_counters(year, last_number) VALUES (%s, 0)", (year,))
            last = 0
        else:
            last = int(row[0])

        new = last + 1
        cur.execute("UPDATE offer_counters SET last_number=%s WHERE year=%s", (new, year))
        number = f"{new:02d}/{year}"
        return new, number

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM offers")
            total_offers = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM offers WHERE status='accepted'")
            accepted_offers = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM offers WHERE status='sent'")
            sent_offers = cur.fetchone()[0]

            cur.execute(
                """
                SELECT o.id, o.number, o.status, o.portal_token, c.name
                FROM offers o
                JOIN clients c ON c.id=o.client_id
                ORDER BY o.created_at DESC
                LIMIT 25
                """
            )
            offers = [
                {"id": r[0], "number": r[1], "status": r[2], "portal_token": r[3], "client_name": r[4]}
                for r in cur.fetchall()
            ]

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "title": "Dashboard",
            "offers": offers,
            "stats": {"total_offers": total_offers, "accepted_offers": accepted_offers, "sent_offers": sent_offers},
        })

@app.get("/clients", response_class=HTMLResponse)
def clients_list(request: Request):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, email, oib FROM clients ORDER BY created_at DESC LIMIT 200")
            clients = [{"id": r[0], "name": r[1], "email": r[2], "oib": r[3]} for r in cur.fetchall()]
    return templates.TemplateResponse("clients.html", {"request": request, "title": "Klijenti", "clients": clients})

@app.get("/clients/new", response_class=HTMLResponse)
def clients_new(request: Request):
    return templates.TemplateResponse("client_new.html", {"request": request, "title": "Novi klijent"})

@app.post("/clients/new")
def clients_new_post(
    name: str = Form(...),
    email: str = Form(None),
    address: str = Form(None),
    oib: str = Form(None),
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO clients(name, email, address, oib) VALUES (%s,%s,%s,%s) RETURNING id",
                (name, email, address, oib),
            )
            cid = cur.fetchone()[0]
            audit(conn, "client_created", "client", cid, {"name": name})
        conn.commit()
    return RedirectResponse(url="/clients", status_code=303)

@app.get("/offers/new", response_class=HTMLResponse)
def offer_new(request: Request):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM clients ORDER BY name ASC")
            clients = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
    return templates.TemplateResponse("offer_new.html", {"request": request, "title": "Nova ponuda", "clients": clients})

def parse_items_blob(items_blob: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for raw in (items_blob or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            # fallback: description only
            items.append({"description": line, "qty": 1, "unit_price": 0})
            continue
        desc, qty, price = parts[0], parts[1], parts[2]
        try:
            qty_v = float(qty.replace(",", "."))
        except Exception:
            qty_v = 1.0
        try:
            price_v = float(price.replace(",", "."))
        except Exception:
            price_v = 0.0
        items.append({"description": desc, "qty": qty_v, "unit_price": price_v})
    if not items:
        raise HTTPException(status_code=400, detail="No items provided")
    return items

@app.post("/offers/new")
def offer_new_post(
    client_id: int = Form(...),
    items: str = Form(...),
    notes: str = Form(None),
):
    with get_conn() as conn:
        year = datetime.now().year
        seq, number = next_offer_number(conn, year)
        token = new_portal_token()

        parsed = parse_items_blob(items)

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO offers(number, year, seq, client_id, status, portal_token, notes) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (number, year, seq, client_id, "sent", token, notes),
            )
            offer_id = cur.fetchone()[0]

            for it in parsed:
                cur.execute(
                    "INSERT INTO offer_items(offer_id, description, qty, unit_price) VALUES (%s,%s,%s,%s)",
                    (offer_id, it["description"], it["qty"], it["unit_price"]),
                )

            audit(conn, "offer_created", "offer", offer_id, {"number": number, "client_id": client_id})
        conn.commit()

    return RedirectResponse(url=f"/offers/{offer_id}", status_code=303)

@app.get("/offers/{offer_id}", response_class=HTMLResponse)
def offer_view(request: Request, offer_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, number, status, portal_token, notes, created_at, client_id FROM offers WHERE id=%s", (offer_id,))
            o = cur.fetchone()
            if not o:
                raise HTTPException(404)
            offer = {"id": o[0], "number": o[1], "status": o[2], "portal_token": o[3], "notes": o[4], "created_at": o[5], "client_id": o[6]}

            cur.execute("SELECT id, name, email, address, oib FROM clients WHERE id=%s", (offer["client_id"],))
            c = cur.fetchone()
            client = {"id": c[0], "name": c[1], "email": c[2], "address": c[3], "oib": c[4]}

            cur.execute("SELECT description, qty, unit_price FROM offer_items WHERE offer_id=%s ORDER BY id ASC", (offer_id,))
            items = []
            total = 0.0
            for r in cur.fetchall():
                line_total = float(r[1]) * float(r[2])
                total += line_total
                items.append({"description": r[0], "qty": float(r[1]), "unit_price": float(r[2]), "line_total": line_total})

    return templates.TemplateResponse("offer_view.html", {"request": request, "title": f"Ponuda {offer['number']}", "offer": offer, "client": client, "items": items, "total": total})

@app.get("/offers/{offer_id}/pdf")
def offer_pdf(offer_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, number, status, portal_token, notes, created_at, client_id FROM offers WHERE id=%s", (offer_id,))
            o = cur.fetchone()
            if not o:
                raise HTTPException(404)
            offer = {"id": o[0], "number": o[1], "status": o[2], "portal_token": o[3], "notes": o[4], "created_at": o[5].date().isoformat(), "client_id": o[6]}

            cur.execute("SELECT id, name, email, address, oib FROM clients WHERE id=%s", (offer["client_id"],))
            c = cur.fetchone()
            client = {"id": c[0], "name": c[1], "email": c[2], "address": c[3], "oib": c[4]}

            cur.execute("SELECT description, qty, unit_price FROM offer_items WHERE offer_id=%s ORDER BY id ASC", (offer_id,))
            items = [{"description": r[0], "qty": float(r[1]), "unit_price": float(r[2])} for r in cur.fetchall()]

        # Generate PDF into memory
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
            build_offer_pdf(tmp.name, offer, client, items)
            tmp.seek(0)
            pdf_bytes = tmp.read()

    headers = {"Content-Disposition": f'inline; filename="ponuda-{offer_id}.pdf"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)

@app.get("/p/{token}", response_class=HTMLResponse)
def portal_view(request: Request, token: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, number, status, portal_token, notes, created_at, client_id FROM offers WHERE portal_token=%s", (token,))
            o = cur.fetchone()
            if not o:
                raise HTTPException(404)
            offer = {"id": o[0], "number": o[1], "status": o[2], "portal_token": o[3], "notes": o[4], "created_at": o[5], "client_id": o[6]}

            cur.execute("SELECT id, name, email, address, oib FROM clients WHERE id=%s", (offer["client_id"],))
            c = cur.fetchone()
            client = {"id": c[0], "name": c[1], "email": c[2], "address": c[3], "oib": c[4]}

            cur.execute("SELECT description, qty, unit_price FROM offer_items WHERE offer_id=%s ORDER BY id ASC", (offer["id"],))
            items = []
            total = 0.0
            for r in cur.fetchall():
                line_total = float(r[1]) * float(r[2])
                total += line_total
                items.append({"description": r[0], "qty": float(r[1]), "unit_price": float(r[2]), "line_total": line_total})

    return templates.TemplateResponse("portal.html", {"request": request, "title": f"Ponuda {offer['number']}", "offer": offer, "client": client, "items": items, "total": total})

@app.get("/p/{token}/accept", response_class=HTMLResponse)
def portal_accept(request: Request, token: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE offers SET status='accepted' WHERE portal_token=%s RETURNING id, number, client_id", (token,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404)
            offer_id, number, client_id = row[0], row[1], row[2]
            audit(conn, "offer_accepted", "offer", offer_id, {"number": number})
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT id, number, status, portal_token FROM offers WHERE id=%s", (offer_id,))
            o = cur.fetchone()
            offer = {"id": o[0], "number": o[1], "status": o[2], "portal_token": o[3]}

    return templates.TemplateResponse("portal_accepted.html", {"request": request, "title": "Ponuda prihvaćena", "offer": offer})
