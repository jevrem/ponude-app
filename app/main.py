import os
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

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


# ---------- Schema compatibility layer ----------
def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s",
            (table,),
        )
        return cur.fetchone() is not None

def _col_exists(conn, table: str, col: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=%s AND column_name=%s",
            (table, col),
        )
        return cur.fetchone() is not None

def schema_mode(conn) -> str:
    # "rel": offers.client_id exists and clients table exists
    # "legacy": offers has client_oib and/or client fields stored on offers
    if _table_exists(conn, "offers") and _col_exists(conn, "offers", "client_id") and _table_exists(conn, "clients"):
        return "rel"
    return "legacy"

def audit(conn, action: str, entity_type: str = None, entity_id: int = None, meta: Dict[str, Any] = None):
    if not _table_exists(conn, "audit_log"):
        return
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log(action, entity_type, entity_id, meta) VALUES (%s,%s,%s,%s)",
            (action, entity_type, entity_id, meta),
        )

def next_offer_number(conn, year: int) -> Tuple[int, str]:
    # If offer_counters exists -> safe concurrent numbering.
    if not _table_exists(conn, "offer_counters"):
        # Fallback: count/max offers (legacy DB compatibility)
        with conn.cursor() as cur:
            if _col_exists(conn, "offers", "year") and _col_exists(conn, "offers", "seq"):
                cur.execute("SELECT COALESCE(MAX(seq),0) FROM offers WHERE year=%s", (year,))
                new = int(cur.fetchone()[0]) + 1
            else:
                cur.execute("SELECT COUNT(*) FROM offers")
                new = int(cur.fetchone()[0]) + 1
        return new, f"{new:02d}/{year}"

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

def _legacy_client_from_offer_row(conn, offer_row: Dict[str, Any]) -> Dict[str, Any]:
    # Build client from offers columns; if clients exists, try match by OIB
    client = {
        "id": None,
        "name": offer_row.get("client_name") or offer_row.get("name") or offer_row.get("company_name") or "",
        "email": offer_row.get("client_email") or offer_row.get("email") or "",
        "address": offer_row.get("client_address") or offer_row.get("address") or "",
        "oib": offer_row.get("client_oib") or offer_row.get("oib") or "",
    }
    if _table_exists(conn, "clients") and client["oib"] and _col_exists(conn, "clients", "oib"):
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, email, address, oib FROM clients WHERE oib=%s LIMIT 1", (client["oib"],))
            r = cur.fetchone()
            if r:
                client = {"id": r[0], "name": r[1], "email": r[2], "address": r[3], "oib": r[4]}
    return client


# ---------- Routes ----------
@app.get("/offer")
def offer_alias():
    # Legacy alias from your old URL
    return RedirectResponse(url="/", status_code=307)

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with get_conn() as conn:
        mode = schema_mode(conn)

        total_offers = accepted_offers = sent_offers = 0
        if _table_exists(conn, "offers"):
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM offers")
                total_offers = cur.fetchone()[0]
                if _col_exists(conn, "offers", "status"):
                    cur.execute("SELECT count(*) FROM offers WHERE status='accepted'")
                    accepted_offers = cur.fetchone()[0]
                    cur.execute("SELECT count(*) FROM offers WHERE status='sent'")
                    sent_offers = cur.fetchone()[0]

        offers = []
        with conn.cursor() as cur:
            if mode == "rel":
                cur.execute(
                    """
                    SELECT o.id, o.number, o.status, o.portal_token, c.name
                    FROM offers o
                    JOIN clients c ON c.id=o.client_id
                    ORDER BY o.created_at DESC
                    LIMIT 25
                    """
                )
                offers = [{"id": r[0], "number": r[1], "status": r[2], "portal_token": r[3], "client_name": r[4]} for r in cur.fetchall()]
            else:
                # Legacy: no FK. Select best-effort columns.
                select_parts = []
                for expr in ["id", "number", "portal_token", "created_at"]:
                    if _col_exists(conn, "offers", expr):
                        select_parts.append(expr)
                if _col_exists(conn, "offers", "status"):
                    select_parts.append("status")
                else:
                    select_parts.append("'sent'::text AS status")

                client_name_col = None
                for c in ["client_name", "name", "company_name"]:
                    if _col_exists(conn, "offers", c):
                        client_name_col = c
                        break
                if client_name_col:
                    select_parts.append(f"{client_name_col} AS client_name")
                else:
                    select_parts.append("''::text AS client_name")

                sql = "SELECT " + ", ".join(select_parts) + " FROM offers ORDER BY created_at DESC NULLS LAST LIMIT 25"
                cur.execute(sql)
                names = [d.name for d in cur.description]
                for r in cur.fetchall():
                    row = dict(zip(names, r))
                    offers.append({
                        "id": row.get("id"),
                        "number": row.get("number") or str(row.get("id")),
                        "status": row.get("status") or "sent",
                        "portal_token": row.get("portal_token") or "",
                        "client_name": row.get("client_name") or "",
                    })

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "title": "Dashboard",
            "offers": offers,
            "stats": {"total_offers": total_offers, "accepted_offers": accepted_offers, "sent_offers": sent_offers},
        })

@app.get("/clients", response_class=HTMLResponse)
def clients_list(request: Request):
    with get_conn() as conn:
        if not _table_exists(conn, "clients"):
            return templates.TemplateResponse("clients.html", {"request": request, "title": "Klijenti", "clients": []})
        with conn.cursor() as cur:
            email_col = "email" if _col_exists(conn, "clients", "email") else "NULL::text AS email"
            oib_col = "oib" if _col_exists(conn, "clients", "oib") else "NULL::text AS oib"
            created_col = "created_at" if _col_exists(conn, "clients", "created_at") else "NULL::timestamptz AS created_at"
            cur.execute(f"SELECT id, name, {email_col}, {oib_col} FROM clients ORDER BY {created_col} DESC NULLS LAST LIMIT 200")
            clients = [{"id": r[0], "name": r[1], "email": r[2], "oib": r[3]} for r in cur.fetchall()]
    return templates.TemplateResponse("clients.html", {"request": request, "title": "Klijenti", "clients": clients})

@app.get("/clients/new", response_class=HTMLResponse)
def clients_new(request: Request):
    with get_conn() as conn:
        if not _table_exists(conn, "clients"):
            raise HTTPException(400, detail="Clients table does not exist in this database.")
    return templates.TemplateResponse("client_new.html", {"request": request, "title": "Novi klijent"})

@app.post("/clients/new")
def clients_new_post(
    name: str = Form(...),
    email: str = Form(None),
    address: str = Form(None),
    oib: str = Form(None),
):
    with get_conn() as conn:
        if not _table_exists(conn, "clients"):
            raise HTTPException(400, detail="Clients table does not exist in this database.")
        with conn.cursor() as cur:
            cols = ["name"]
            vals = [name]
            if _col_exists(conn, "clients", "email"):
                cols.append("email"); vals.append(email)
            if _col_exists(conn, "clients", "address"):
                cols.append("address"); vals.append(address)
            if _col_exists(conn, "clients", "oib"):
                cols.append("oib"); vals.append(oib)

            placeholders = ", ".join(["%s"] * len(cols))
            cur.execute(f"INSERT INTO clients({', '.join(cols)}) VALUES ({placeholders}) RETURNING id", tuple(vals))
            cid = cur.fetchone()[0]
            audit(conn, "client_created", "client", cid, {"name": name})
        conn.commit()
    return RedirectResponse(url="/clients", status_code=303)

@app.get("/offers/new", response_class=HTMLResponse)
def offer_new(request: Request):
    with get_conn() as conn:
        mode = schema_mode(conn)
        clients = []
        if mode == "rel":
            with conn.cursor() as cur:
                cur.execute("SELECT id, name FROM clients ORDER BY name ASC")
                clients = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
        else:
            clients = [{"id": 0, "name": "Legacy DB (bez FK klijenata)"}]
    return templates.TemplateResponse("offer_new.html", {"request": request, "title": "Nova ponuda", "clients": clients})

def parse_items_blob(items_blob: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for raw in (items_blob or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
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
            if _col_exists(conn, "offers", "client_id"):
                cur.execute(
                    "INSERT INTO offers(number, year, seq, client_id, status, portal_token, notes) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (number, year, seq, client_id, "sent", token, notes),
                )
            else:
                cols = []
                vals = []
                for c, v in [
                    ("number", number),
                    ("year", year),
                    ("seq", seq),
                    ("status", "sent"),
                    ("portal_token", token),
                    ("notes", notes),
                ]:
                    if _col_exists(conn, "offers", c):
                        cols.append(c); vals.append(v)
                if not cols:
                    raise HTTPException(500, detail="Offers table schema is not compatible.")
                placeholders = ", ".join(["%s"] * len(cols))
                cur.execute(f"INSERT INTO offers({', '.join(cols)}) VALUES ({placeholders}) RETURNING id", tuple(vals))

            offer_id = cur.fetchone()[0]

            if not _table_exists(conn, "offer_items"):
                raise HTTPException(500, detail="offer_items table missing.")
            for it in parsed:
                cur.execute(
                    "INSERT INTO offer_items(offer_id, description, qty, unit_price) VALUES (%s,%s,%s,%s)",
                    (offer_id, it["description"], it["qty"], it["unit_price"]),
                )

            audit(conn, "offer_created", "offer", offer_id, {"number": number})
        conn.commit()

    return RedirectResponse(url=f"/offers/{offer_id}", status_code=303)

def _load_offer(conn, offer_id: Optional[int] = None, token: Optional[str] = None) -> Dict[str, Any]:
    with conn.cursor() as cur:
        if token is not None:
            cur.execute("SELECT * FROM offers WHERE portal_token=%s LIMIT 1", (token,))
        else:
            cur.execute("SELECT * FROM offers WHERE id=%s LIMIT 1", (offer_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404)
        names = [d.name for d in cur.description]
        return dict(zip(names, row))

def _load_items(conn, offer_id: int) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT description, qty, unit_price FROM offer_items WHERE offer_id=%s ORDER BY id ASC", (offer_id,))
        items = []
        for r in cur.fetchall():
            items.append({"description": r[0], "qty": float(r[1]), "unit_price": float(r[2])})
        return items

@app.get("/offers/{offer_id}", response_class=HTMLResponse)
def offer_view(request: Request, offer_id: int):
    with get_conn() as conn:
        mode = schema_mode(conn)
        offer_row = _load_offer(conn, offer_id=offer_id)
        offer = {
            "id": offer_row.get("id"),
            "number": offer_row.get("number") or str(offer_row.get("id")),
            "status": offer_row.get("status") or "sent",
            "portal_token": offer_row.get("portal_token") or "",
            "notes": offer_row.get("notes"),
            "created_at": offer_row.get("created_at"),
        }

        if mode == "rel":
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, email, address, oib FROM clients WHERE id=%s", (offer_row["client_id"],))
                c = cur.fetchone()
                client = {"id": c[0], "name": c[1], "email": c[2], "address": c[3], "oib": c[4]}
        else:
            client = _legacy_client_from_offer_row(conn, offer_row)

        raw_items = _load_items(conn, int(offer["id"]))
        items = []
        total = 0.0
        for it in raw_items:
            line_total = float(it["qty"]) * float(it["unit_price"])
            total += line_total
            items.append({**it, "line_total": line_total})

    return templates.TemplateResponse("offer_view.html", {"request": request, "title": f"Ponuda {offer['number']}", "offer": offer, "client": client, "items": items, "total": total})

@app.get("/offers/{offer_id}/pdf")
def offer_pdf(offer_id: int):
    import tempfile
    with get_conn() as conn:
        mode = schema_mode(conn)
        offer_row = _load_offer(conn, offer_id=offer_id)
        offer = {
            "id": offer_row.get("id"),
            "number": offer_row.get("number") or str(offer_row.get("id")),
            "status": offer_row.get("status") or "sent",
            "portal_token": offer_row.get("portal_token") or "",
            "notes": offer_row.get("notes"),
            "created_at": (offer_row.get("created_at").date().isoformat() if offer_row.get("created_at") else datetime.now().date().isoformat()),
        }

        if mode == "rel":
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, email, address, oib FROM clients WHERE id=%s", (offer_row["client_id"],))
                c = cur.fetchone()
                client = {"id": c[0], "name": c[1], "email": c[2], "address": c[3], "oib": c[4]}
        else:
            client = _legacy_client_from_offer_row(conn, offer_row)

        items = _load_items(conn, int(offer["id"]))

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
            build_offer_pdf(tmp.name, offer, client, items)
            tmp.seek(0)
            pdf_bytes = tmp.read()

    headers = {"Content-Disposition": f'inline; filename="ponuda-{offer_id}.pdf"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)

@app.get("/p/{token}", response_class=HTMLResponse)
def portal_view(request: Request, token: str):
    with get_conn() as conn:
        mode = schema_mode(conn)
        offer_row = _load_offer(conn, token=token)
        offer = {
            "id": offer_row.get("id"),
            "number": offer_row.get("number") or str(offer_row.get("id")),
            "status": offer_row.get("status") or "sent",
            "portal_token": offer_row.get("portal_token") or token,
            "notes": offer_row.get("notes"),
        }
        if mode == "rel":
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, email, address, oib FROM clients WHERE id=%s", (offer_row["client_id"],))
                c = cur.fetchone()
                client = {"id": c[0], "name": c[1], "email": c[2], "address": c[3], "oib": c[4]}
        else:
            client = _legacy_client_from_offer_row(conn, offer_row)

        raw_items = _load_items(conn, int(offer["id"]))
        items = []
        total = 0.0
        for it in raw_items:
            line_total = float(it["qty"]) * float(it["unit_price"])
            total += line_total
            items.append({**it, "line_total": line_total})

    return templates.TemplateResponse("portal.html", {"request": request, "title": f"Ponuda {offer['number']}", "offer": offer, "client": client, "items": items, "total": total})

@app.get("/p/{token}/accept", response_class=HTMLResponse)
def portal_accept(request: Request, token: str):
    with get_conn() as conn:
        if not _col_exists(conn, "offers", "status"):
            raise HTTPException(400, detail="This database schema does not support offer status.")
        with conn.cursor() as cur:
            cur.execute("UPDATE offers SET status='accepted' WHERE portal_token=%s RETURNING id, number", (token,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404)
            offer_id, number = row[0], row[1]
            audit(conn, "offer_accepted", "offer", offer_id, {"number": number})
        conn.commit()

        offer = {"id": offer_id, "number": number, "status": "accepted", "portal_token": token}
    return templates.TemplateResponse("portal_accepted.html", {"request": request, "title": "Ponuda prihvaćena", "offer": offer})
