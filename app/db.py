from __future__ import annotations

import os, io, json, sqlite3, tempfile
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import openpyxl
from openpyxl.utils import get_column_letter

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Optional Postgres (Neon)
try:
    import psycopg
except Exception:
    psycopg = None  # type: ignore


_DB_KIND = None
_SQLITE_PATH = os.getenv("SQLITE_PATH") or os.path.join(tempfile.gettempdir(), "ponude.sqlite3")


def _pg_dsn() -> str | None:
    return (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or "").strip() or None


def init() -> None:
    global _DB_KIND
    dsn = _pg_dsn()
    if dsn and psycopg is not None:
        _DB_KIND = "pg"
        _pg_exec("""
        CREATE TABLE IF NOT EXISTS offers (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            client_name TEXT,
            status TEXT NOT NULL DEFAULT 'DRAFT',
            place TEXT, delivery TEXT, payment TEXT, note TEXT, signature TEXT,
            vat_rate INT NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS items (
            id SERIAL PRIMARY KEY,
            offer_id INT NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            qty NUMERIC NOT NULL DEFAULT 1,
            price NUMERIC NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (
            username TEXT PRIMARY KEY,
            data JSONB NOT NULL
        );
        """)
        return

    _DB_KIND = "sqlite"
    con = sqlite3.connect(_SQLITE_PATH)
    cur = con.cursor()
    cur.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        created_at TEXT NOT NULL,
        client_name TEXT,
        status TEXT NOT NULL DEFAULT 'DRAFT',
        place TEXT, delivery TEXT, payment TEXT, note TEXT, signature TEXT,
        vat_rate INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        offer_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        qty REAL NOT NULL DEFAULT 1,
        price REAL NOT NULL DEFAULT 0,
        FOREIGN KEY(offer_id) REFERENCES offers(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS settings (
        username TEXT PRIMARY KEY,
        data TEXT NOT NULL
    );
    """)
    con.commit()
    con.close()


def _pg_exec(sql: str, params: tuple = ()) -> None:
    assert psycopg is not None
    dsn = _pg_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    with psycopg.connect(dsn) as con:
        with con.cursor() as cur:
            cur.execute(sql, params)
        con.commit()


def _pg_fetchall(sql: str, params: tuple = ()) -> list[tuple]:
    assert psycopg is not None
    dsn = _pg_dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    with psycopg.connect(dsn) as con:
        with con.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _pg_fetchone(sql: str, params: tuple = ()) -> tuple | None:
    rows = _pg_fetchall(sql, params)
    return rows[0] if rows else None


def _sqlite() -> sqlite3.Connection:
    con = sqlite3.connect(_SQLITE_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    return con


def create_offer(user: str) -> int:
    if _DB_KIND == "pg":
        row = _pg_fetchone("INSERT INTO offers(username, created_at) VALUES (%s, NOW()) RETURNING id", (user,))
        assert row is not None
        return int(row[0])
    con = _sqlite()
    cur = con.cursor()
    cur.execute("INSERT INTO offers(username, created_at) VALUES (?,?)", (user, datetime.now().isoformat(timespec="minutes")))
    con.commit()
    oid = int(cur.lastrowid)
    con.close()
    return oid


def get_offer(offer_id: int) -> dict[str, Any] | None:
    if _DB_KIND == "pg":
        row = _pg_fetchone("SELECT id, username, created_at, client_name, status, place, delivery, payment, note, signature, vat_rate FROM offers WHERE id=%s", (offer_id,))
        if not row:
            return None
        keys = ["id","username","created_at","client_name","status","place","delivery","payment","note","signature","vat_rate"]
        return dict(zip(keys, row))
    con = _sqlite()
    cur = con.cursor()
    cur.execute("SELECT * FROM offers WHERE id=?", (offer_id,))
    r = cur.fetchone()
    con.close()
    return dict(r) if r else None


def update_offer_fields(offer_id: int, fields: dict[str, Any]) -> None:
    if not fields:
        return
    if _DB_KIND == "pg":
        sets=[]
        params=[]
        for k,v in fields.items():
            sets.append(f"{k}=%s")
            params.append(v)
        params.append(offer_id)
        _pg_exec(f"UPDATE offers SET {', '.join(sets)} WHERE id=%s", tuple(params))
        return
    con=_sqlite()
    cur=con.cursor()
    sets=[]
    params=[]
    for k,v in fields.items():
        sets.append(f"{k}=?")
        params.append(v)
    params.append(offer_id)
    cur.execute(f"UPDATE offers SET {', '.join(sets)} WHERE id=?", params)
    con.commit(); con.close()


def add_item(offer_id: int, name: str, qty: float, price: float) -> None:
    if _DB_KIND == "pg":
        _pg_exec("INSERT INTO items(offer_id,name,qty,price) VALUES (%s,%s,%s,%s)", (offer_id,name,qty,price))
        return
    con=_sqlite()
    con.execute("INSERT INTO items(offer_id,name,qty,price) VALUES (?,?,?,?)", (offer_id,name,qty,price))
    con.commit(); con.close()


def delete_item(offer_id: int, item_id: int) -> None:
    if _DB_KIND == "pg":
        _pg_exec("DELETE FROM items WHERE id=%s AND offer_id=%s", (item_id, offer_id))
        return
    con=_sqlite()
    con.execute("DELETE FROM items WHERE id=? AND offer_id=?", (item_id, offer_id))
    con.commit(); con.close()


def clear_items(offer_id: int) -> None:
    if _DB_KIND == "pg":
        _pg_exec("DELETE FROM items WHERE offer_id=%s", (offer_id,))
        return
    con=_sqlite()
    con.execute("DELETE FROM items WHERE offer_id=?", (offer_id,))
    con.commit(); con.close()


def list_items(offer_id: int) -> list[dict[str, Any]]:
    if _DB_KIND == "pg":
        rows = _pg_fetchall("SELECT id,name,qty,price FROM items WHERE offer_id=%s ORDER BY id", (offer_id,))
        out=[]
        for r in rows:
            out.append({"id": r[0], "name": r[1], "qty": float(r[2]), "price": float(r[3])})
        return out
    con=_sqlite()
    cur=con.cursor()
    cur.execute("SELECT id,name,qty,price FROM items WHERE offer_id=? ORDER BY id", (offer_id,))
    rows=cur.fetchall(); con.close()
    return [{"id": r[0], "name": r[1], "qty": float(r[2]), "price": float(r[3])} for r in rows]


def list_offers(user: str) -> list[dict[str, Any]]:
    if _DB_KIND == "pg":
        rows=_pg_fetchall("SELECT id, created_at, client_name, status, vat_rate FROM offers WHERE username=%s ORDER BY id DESC", (user,))
        return [{"id": r[0], "created_at": str(r[1])[:16], "client_name": r[2] or "", "status": r[3], "vat_rate": int(r[4] or 0)} for r in rows]
    con=_sqlite()
    cur=con.cursor()
    cur.execute("SELECT id, created_at, client_name, status, vat_rate FROM offers WHERE username=? ORDER BY id DESC", (user,))
    rows=cur.fetchall(); con.close()
    return [{"id": r[0], "created_at": r[1], "client_name": r[2] or "", "status": r[3], "vat_rate": int(r[4] or 0)} for r in rows]


def get_settings(user: str) -> dict[str, Any] | None:
    if _DB_KIND == "pg":
        row=_pg_fetchone("SELECT data FROM settings WHERE username=%s", (user,))
        if not row:
            return None
        return dict(row[0] or {})
    con=_sqlite()
    cur=con.cursor()
    cur.execute("SELECT data FROM settings WHERE username=?", (user,))
    r=cur.fetchone(); con.close()
    if not r:
        return None
    try:
        return json.loads(r[0])
    except Exception:
        return None


def save_settings(user: str, data: dict[str, Any]) -> None:
    data = {k: (v or "") for k,v in data.items()}
    if _DB_KIND == "pg":
        _pg_exec(
            "INSERT INTO settings(username,data) VALUES (%s,%s) ON CONFLICT (username) DO UPDATE SET data=EXCLUDED.data",
            (user, json.dumps(data)),
        )
        return
    con=_sqlite()
    con.execute(
        "INSERT INTO settings(username,data) VALUES (?,?) ON CONFLICT(username) DO UPDATE SET data=excluded.data",
        (user, json.dumps(data)),
    )
    con.commit(); con.close()


def _register_font(static_dir: str) -> str:
    # Use bundled DejaVuSans.ttf for Croatian chars
    font_path = Path(static_dir) / "DejaVuSans.ttf"
    if font_path.exists():
        try:
            pdfmetrics.registerFont(TTFont("DejaVuSans", str(font_path)))
            return "DejaVuSans"
        except Exception:
            pass
    return "Helvetica"


def render_offer_pdf(offer: dict[str, Any], items: list[dict[str, Any]], settings: dict[str, Any], static_dir: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    font = _register_font(static_dir)
    c.setFont(font, 16)
    c.drawString(40, h-50, "Ponuda")

    c.setFont(font, 10)
    c.drawString(40, h-70, f"Klijent: {offer.get('client_name') or ''}")
    c.drawString(40, h-85, f"Datum: {str(offer.get('created_at') or '')[:16]}")

    y = h-120
    c.setFont(font, 11)
    c.drawString(40, y, "Stavke:")
    y -= 18
    c.setFont(font, 10)
    subtotal = Decimal("0")
    for it in items:
        line_total = Decimal(str(it.get("qty",0))) * Decimal(str(it.get("price",0)))
        subtotal += line_total
        c.drawString(40, y, f"- {it.get('name','')}")
        c.drawRightString(w-220, y, f"{Decimal(str(it.get('qty',0))):.2f}")
        c.drawRightString(w-140, y, f"{Decimal(str(it.get('price',0))):.2f}")
        c.drawRightString(w-40, y, f"{line_total:.2f}")
        y -= 14
        if y < 80:
            c.showPage()
            c.setFont(font, 10)
            y = h-50

    vat_rate = Decimal(str(offer.get("vat_rate",0) or 0))
    vat = (subtotal * vat_rate / Decimal("100")) if vat_rate else Decimal("0")
    total = subtotal + vat

    y -= 10
    c.line(40, y, w-40, y)
    y -= 16
    c.drawRightString(w-40, y, f"Međuzbroj: {subtotal:.2f} €")
    y -= 14
    c.drawRightString(w-40, y, f"PDV {vat_rate:.0f}%: {vat:.2f} €")
    y -= 14
    c.setFont(font, 12)
    c.drawRightString(w-40, y, f"Ukupno: {total:.2f} €")

    # meta
    c.setFont(font, 10)
    y -= 30
    for label, key in [("Mjesto", "place"), ("Rok isporuke", "delivery"), ("Rok plaćanja", "payment"), ("Napomena", "note"), ("Potpis", "signature")]:
        val = offer.get(key) or ""
        if val:
            c.drawString(40, y, f"{label}: {val}")
            y -= 14

    c.showPage()
    c.save()
    return buf.getvalue()


def render_offer_excel(offer: dict[str, Any], items: list[dict[str, Any]]) -> bytes:
    wb=openpyxl.Workbook()
    ws=wb.active
    ws.title="Ponuda"
    ws.append(["Naziv", "Količina", "Cijena", "Ukupno"])
    for it in items:
        qty=float(it.get("qty",0) or 0)
        price=float(it.get("price",0) or 0)
        ws.append([it.get("name",""), qty, price, qty*price])
    # autosize
    for col in range(1,5):
        ws.column_dimensions[get_column_letter(col)].width = 20
    buf=io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
