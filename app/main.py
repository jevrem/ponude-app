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


def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s", (table,))
        return cur.fetchone() is not None


def _col_exists(conn, table: str, col: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=%s AND column_name=%s", (table, col))
        return cur.fetchone() is not None


def schema_mode(conn) -> str:
    if _table_exists(conn, "offers") and _col_exists(conn, "offers", "client_id") and _table_exists(conn, "clients"):
        return "rel"
    return "legacy"


def audit(conn, action: str, entity_type: str = None, entity_id: int = None, meta: Dict[str, Any] = None):
    if not _table_exists(conn, "audit_log"):
        return
    with conn.cursor() as cur:
        cur.execute("INSERT INTO audit_log(action, entity_type, entity_id, meta) VALUES (%s,%s,%s,%s)", (action, entity_type, entity_id, meta))


def ensure_min_schema(conn):
    if not _table_exists(conn, "offers"):
        return
    with conn.cursor() as cur:
        if not _col_exists(conn, "offers", "status"):
            cur.execute("ALTER TABLE offers ADD COLUMN status TEXT DEFAULT 'sent'")
        if not _col_exists(conn, "offers", "portal_token"):
            cur.execute("ALTER TABLE offers ADD COLUMN portal_token TEXT")
        if not _col_exists(conn, "offers", "vat_rate"):
            cur.execute("ALTER TABLE offers ADD COLUMN vat_rate NUMERIC(6,2) DEFAULT 25")
        if not _col_exists(conn, "offers", "prices_include_vat"):
            cur.execute("ALTER TABLE offers ADD COLUMN prices_include_vat BOOLEAN DEFAULT TRUE")
    conn.commit()


@app.on_event("startup")
def _startup():
    with get_conn() as conn:
        ensure_min_schema(conn)


def next_offer_number(conn, year: int) -> Tuple[int, str]:
    if not _table_exists(conn, "offer_counters"):
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
        return new, f"{new:02d}/{year}"


def _legacy_client_from_offer_row(conn, offer_row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": None,
        "name": offer_row.get("client_name") or offer_row.get("name") or offer_row.get("company_name") or "",
        "email": offer_row.get("client_email") or offer_row.get("email") or "",
        "address": offer_row.get("client_address") or offer_row.get("address") or "",
        "oib": offer_row.get("client_oib") or offer_row.get("oib") or "",
    }


def compute_totals(items: List[Dict[str, Any]], vat_rate: float, prices_include_vat: bool) -> Dict[str, float]:
    total = sum(float(it["qty"]) * float(it["unit_price"]) for it in items)
    vr = max(0.0, float(vat_rate)) / 100.0
    if prices_include_vat:
        subtotal = total / (1.0 + vr) if vr > 0 else total
        vat_amount = total - subtotal
        return {"subtotal": subtotal, "vat_amount": vat_amount, "total": total}
    subtotal = total
    vat_amount = subtotal * vr
    return {"subtotal": subtotal, "vat_amount": vat_amount, "total": subtotal + vat_amount}


@app.get("/offer")
def offer_alias():
    return RedirectResponse(url="/", status_code=307)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with get_conn() as conn:
        ensure_min_schema(conn)
        mode = schema_mode(conn)

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM offers")
            total_offers = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM offers WHERE status='accepted'")
            accepted_offers = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM offers WHERE status='sent'")
            sent_offers = cur.fetchone()[0]

        offers = []
        with conn.cursor() as cur:
            if mode == "rel":
                cur.execute("""SELECT o.id, o.number, o.status, o.portal_token, c.name
                               FROM offers o JOIN clients c ON c.id=o.client_id
                               ORDER BY o.created_at DESC LIMIT 25""")
                offers = [{"id": r[0], "number": r[1], "status": r[2], "portal_token": r[3], "client_name": r[4]} for r in cur.fetchall()]
            else:
                cur.execute("SELECT id, COALESCE(number, id::text) AS number, status, COALESCE(portal_token,'') AS portal_token, ''::text AS client_name FROM offers ORDER BY created_at DESC NULLS LAST LIMIT 25")
                offers = [{"id": r[0], "number": r[1], "status": r[2], "portal_token": r[3], "client_name": r[4]} for r in cur.fetchall()]

    return templates.TemplateResponse("dashboard.html", {"request": request, "title": "Dashboard", "offers": offers, "stats": {"total_offers": total_offers, "accepted_offers": accepted_offers, "sent_offers": sent_offers}})


@app.get("/clients", response_class=HTMLResponse)
def clients_list(request: Request):
    with get_conn() as conn:
        if not _table_exists(conn, "clients"):
            return templates.TemplateResponse("clients.html", {"request": request, "title": "Klijenti", "clients": []})
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, email, oib FROM clients ORDER BY created_at DESC NULLS LAST LIMIT 200")
            clients = [{"id": r[0], "name": r[1], "email": r[2], "oib": r[3]} for r in cur.fetchall()]
    return templates.TemplateResponse("clients.html", {"request": request, "title": "Klijenti", "clients": clients})


@app.get("/clients/new", response_class=HTMLResponse)
def clients_new(request: Request):
    return templates.TemplateResponse("client_new.html", {"request": request, "title": "Novi klijent"})


@app.post("/clients/new")
def clients_new_post(name: str = Form(...), email: str = Form(None), address: str = Form(None), oib: str = Form(None)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO clients(name, email, address, oib) VALUES (%s,%s,%s,%s) RETURNING id", (name, email, address, oib))
            cid = cur.fetchone()[0]
            audit(conn, "client_created", "client", cid, {"name": name})
        conn.commit()
    return RedirectResponse(url="/clients", status_code=303)


@app.get("/offers/new", response_class=HTMLResponse)
def offer_new(request: Request):
    with get_conn() as conn:
        ensure_min_schema(conn)
        mode = schema_mode(conn)
        if mode == "rel":
            with conn.cursor() as cur:
                cur.execute("SELECT id, name FROM clients ORDER BY name ASC")
                clients = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
        else:
            clients = [{"id": 0, "name": "Legacy DB (bez FK klijenata)"}]
    return templates.TemplateResponse("offer_new.html", {"request": request, "title": "Nova ponuda", "clients": clients})


def parse_items_from_form(item_desc: List[str], item_qty: List[str], item_price: List[str], legacy_blob: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if item_desc and any((d or "").strip() for d in item_desc):
        for d, q, p in zip(item_desc, item_qty or [], item_price or []):
            if not (d or "").strip():
                continue
            try:
                qty_v = float((q or "1").replace(",", "."))
            except Exception:
                qty_v = 1.0
            try:
                price_v = float((p or "0").replace(",", "."))
            except Exception:
                price_v = 0.0
            items.append({"description": d.strip(), "qty": qty_v, "unit_price": price_v})
        if items:
            return items

    for raw in (legacy_blob or "").splitlines():
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
    vat_rate: float = Form(25),
    prices_include_vat: int = Form(1),
    notes: str = Form(None),
    item_desc: List[str] = Form(default=[]),
    item_qty: List[str] = Form(default=[]),
    item_price: List[str] = Form(default=[]),
    items: str = Form(None),
):
    with get_conn() as conn:
        ensure_min_schema(conn)
        year = datetime.now().year
        seq, number = next_offer_number(conn, year)
        token = new_portal_token()
        parsed = parse_items_from_form(item_desc, item_qty, item_price, items)
        piv = bool(int(prices_include_vat))

        with conn.cursor() as cur:
            if _col_exists(conn, "offers", "client_id"):
                cur.execute("INSERT INTO offers(number, year, seq, client_id, status, portal_token, notes, vat_rate, prices_include_vat) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                            (number, year, seq, client_id, "sent", token, notes, vat_rate, piv))
            else:
                cols = []
                vals = []
                for c, v in [("number", number), ("year", year), ("seq", seq), ("status", "sent"), ("portal_token", token), ("notes", notes), ("vat_rate", vat_rate), ("prices_include_vat", piv)]:
                    if _col_exists(conn, "offers", c):
                        cols.append(c); vals.append(v)
                placeholders = ", ".join(["%s"] * len(cols))
                cur.execute(f"INSERT INTO offers({', '.join(cols)}) VALUES ({placeholders}) RETURNING id", tuple(vals))
            offer_id = cur.fetchone()[0]

            for it in parsed:
                cur.execute("INSERT INTO offer_items(offer_id, description, qty, unit_price) VALUES (%s,%s,%s,%s)", (offer_id, it["description"], it["qty"], it["unit_price"]))

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
        return [{"description": r[0], "qty": float(r[1]), "unit_price": float(r[2])} for r in cur.fetchall()]


@app.get("/offers/{offer_id}", response_class=HTMLResponse)
def offer_view(request: Request, offer_id: int):
    with get_conn() as conn:
        ensure_min_schema(conn)
        mode = schema_mode(conn)
        offer_row = _load_offer(conn, offer_id=offer_id)

        vat_rate = float(offer_row.get("vat_rate") or 25)
        piv = bool(offer_row.get("prices_include_vat") if offer_row.get("prices_include_vat") is not None else True)

        offer = {"id": offer_row.get("id"), "number": offer_row.get("number") or str(offer_row.get("id")), "status": offer_row.get("status") or "sent",
                 "portal_token": offer_row.get("portal_token") or "", "notes": offer_row.get("notes"), "created_at": offer_row.get("created_at"),
                 "vat_rate": vat_rate, "prices_include_vat": piv}

        if mode == "rel":
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, email, address, oib FROM clients WHERE id=%s", (offer_row["client_id"],))
                c = cur.fetchone()
                client = {"id": c[0], "name": c[1], "email": c[2], "address": c[3], "oib": c[4]}
        else:
            client = _legacy_client_from_offer_row(conn, offer_row)

        raw_items = _load_items(conn, int(offer["id"]))
        items = [{**it, "line_total": float(it["qty"]) * float(it["unit_price"])} for it in raw_items]
        totals = compute_totals(raw_items, vat_rate, piv)

    return templates.TemplateResponse("offer_view.html", {"request": request, "title": f"Ponuda {offer['number']}", "offer": offer, "client": client, "items": items, "totals": totals})


@app.get("/offers/{offer_id}/pdf")
def offer_pdf(offer_id: int):
    import tempfile
    with get_conn() as conn:
        ensure_min_schema(conn)
        mode = schema_mode(conn)
        offer_row = _load_offer(conn, offer_id=offer_id)

        vat_rate = float(offer_row.get("vat_rate") or 25)
        piv = bool(offer_row.get("prices_include_vat") if offer_row.get("prices_include_vat") is not None else True)

        offer = {"id": offer_row.get("id"), "number": offer_row.get("number") or str(offer_row.get("id")), "status": offer_row.get("status") or "sent",
                 "portal_token": offer_row.get("portal_token") or "", "notes": offer_row.get("notes"),
                 "created_at": (offer_row.get("created_at").date().isoformat() if offer_row.get("created_at") else datetime.now().date().isoformat()),
                 "vat_rate": vat_rate, "prices_include_vat": piv}

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

    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="ponuda-{offer_id}.pdf"'})


@app.get("/p/{token}", response_class=HTMLResponse)
def portal_view(request: Request, token: str):
    with get_conn() as conn:
        ensure_min_schema(conn)
        mode = schema_mode(conn)
        offer_row = _load_offer(conn, token=token)

        vat_rate = float(offer_row.get("vat_rate") or 25)
        piv = bool(offer_row.get("prices_include_vat") if offer_row.get("prices_include_vat") is not None else True)

        offer = {"id": offer_row.get("id"), "number": offer_row.get("number") or str(offer_row.get("id")), "status": offer_row.get("status") or "sent",
                 "portal_token": offer_row.get("portal_token") or token, "notes": offer_row.get("notes"),
                 "vat_rate": vat_rate, "prices_include_vat": piv}

        if mode == "rel":
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, email, address, oib FROM clients WHERE id=%s", (offer_row["client_id"],))
                c = cur.fetchone()
                client = {"id": c[0], "name": c[1], "email": c[2], "address": c[3], "oib": c[4]}
        else:
            client = _legacy_client_from_offer_row(conn, offer_row)

        raw_items = _load_items(conn, int(offer["id"]))
        items = [{**it, "line_total": float(it["qty"]) * float(it["unit_price"])} for it in raw_items]
        totals = compute_totals(raw_items, vat_rate, piv)

    return templates.TemplateResponse("portal.html", {"request": request, "title": f"Ponuda {offer['number']}", "offer": offer, "client": client, "items": items, "totals": totals})


@app.get("/p/{token}/accept", response_class=HTMLResponse)
def portal_accept(request: Request, token: str):
    with get_conn() as conn:
        ensure_min_schema(conn)
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
