from __future__ import annotations

import io
import os
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import openpyxl
from openpyxl.utils import get_column_letter

import psycopg
from psycopg.rows import dict_row

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


# -----------------------------
# Connection / bootstrap
# -----------------------------

def _db_url() -> str:
    # Render/Neon typically provide DATABASE_URL; keep POSTGRES_URL as fallback
    url = (os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


@contextmanager
def get_conn():
    with psycopg.connect(_db_url(), row_factory=dict_row) as conn:
        yield conn


def init_db() -> None:
    """
    Idempotent schema init for Postgres.
    Safe to run on every startup.
    """
    with get_conn() as conn:
        conn.execute(
            """
            create table if not exists offers (
              id bigserial primary key,
              user_name text not null,
              client_name text,
              created_at timestamptz not null default now(),

              -- numbering
              offer_year int,
              offer_seq int,
              offer_no text,

              -- workflow
              status text not null default 'DRAFT',
              accepted_at timestamptz,

              -- meta for PDF
              terms_delivery text,
              terms_payment text,
              note text,
              place text,
              signed_by text,
              vat_rate double precision not null default 0
            );
            """
        )

        conn.execute(
            """
            create table if not exists offer_items (
              id bigserial primary key,
              offer_id bigint not null references offers(id) on delete cascade,
              name text not null,
              qty double precision not null default 1,
              price double precision not null default 0,
              line_total double precision not null default 0,
              created_at timestamptz not null default now()
            );
            """
        )

        conn.execute(
            """
            create table if not exists company_settings (
              user_name text primary key,
              company_name text,
              company_address text,
              company_oib text,
              company_iban text,
              company_email text,
              company_phone text,
              logo_path text,
              created_at timestamptz not null default now(),
              updated_at timestamptz not null default now()
            );
            """
        )

        conn.execute(
            """
            create table if not exists clients (
              id bigserial primary key,
              user_name text not null,
              name text not null,
              created_at timestamptz not null default now()
            );
            """
        )

        # Indexes
        conn.execute("create index if not exists idx_offers_user_name on offers(user_name);")
        conn.execute("create index if not exists idx_offer_items_offer_id on offer_items(offer_id);")
        conn.execute("create unique index if not exists idx_clients_user_name_name on clients(user_name, name);")
        conn.execute("create index if not exists idx_offers_user_year_seq on offers(user_name, offer_year, offer_seq);")

        # Backfill / ensure defaults
        conn.execute("update offers set vat_rate=0 where vat_rate is null;")
        conn.execute("update offers set status='DRAFT' where status is null;")
        try:
            conn.execute("alter table offers alter column status set default 'DRAFT';")
        except Exception:
            pass
        try:
            conn.execute("alter table offers alter column status set not null;")
        except Exception:
            pass


# -----------------------------
# Offers
# -----------------------------

def create_offer(user: str, client_name: str | None = None) -> int:
    year = datetime.now().year
    with get_conn() as conn:
        row_seq = conn.execute(
            """
            select coalesce(max(offer_seq), 0) as max_seq
            from offers
            where user_name=%s and offer_year=%s
            """,
            (user, year),
        ).fetchone()
        next_seq = int((row_seq or {}).get("max_seq") or 0) + 1
        offer_no = f"{year}-{next_seq:04d}"

        row = conn.execute(
            """
            insert into offers(user_name, client_name, offer_year, offer_seq, offer_no, status, vat_rate)
            values (%s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (user, client_name, year, next_seq, offer_no, "DRAFT", 0),
        ).fetchone()
        return int(row["id"])


def get_offer(user: str, offer_id: int):
    with get_conn() as conn:
        return conn.execute(
            """
            select id, user_name, client_name, created_at, offer_no, offer_year, offer_seq,
                   status, accepted_at,
                   terms_delivery, terms_payment, note, place, signed_by, vat_rate
            from offers
            where id=%s and user_name=%s
            """,
            (offer_id, user),
        ).fetchone()


def update_offer_client_name(user: str, offer_id: int, client_name: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "update offers set client_name=%s where id=%s and user_name=%s",
            (client_name, offer_id, user),
        )


def update_offer_status(user: str, offer_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "update offers set status=%s where id=%s and user_name=%s",
            (status, offer_id, user),
        )


def accept_offer(user: str, offer_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "update offers set status='ACCEPTED', accepted_at=now() where id=%s and user_name=%s",
            (offer_id, user),
        )


def update_offer_meta(
    user: str,
    offer_id: int,
    terms_delivery: str | None,
    terms_payment: str | None,
    note: str | None,
    place: str | None,
    signed_by: str | None,
    vat_rate: float | None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            update offers
            set terms_delivery=%s,
                terms_payment=%s,
                note=%s,
                place=%s,
                signed_by=%s,
                vat_rate=%s
            where id=%s and user_name=%s
            """,
            (terms_delivery, terms_payment, note, place, signed_by, float(vat_rate or 0), offer_id, user),
        )


# -----------------------------
# Items
# -----------------------------

def add_item(offer_id: int, name: str, qty: float, price: float) -> None:
    q = float(qty or 0)
    p = float(price or 0)
    line_total = q * p
    with get_conn() as conn:
        conn.execute(
            """
            insert into offer_items(offer_id, name, qty, price, line_total)
            values (%s, %s, %s, %s, %s)
            """,
            (offer_id, (name or "").strip(), q, p, line_total),
        )


def list_items(offer_id: int):
    with get_conn() as conn:
        return conn.execute(
            """
            select id, name, qty, price, line_total
            from offer_items
            where offer_id=%s
            order by id asc
            """,
            (offer_id,),
        ).fetchall()


def delete_item(offer_id: int, item_id: int) -> None:
    with get_conn() as conn:
        conn.execute("delete from offer_items where offer_id=%s and id=%s", (offer_id, item_id))


def clear_items(offer_id: int) -> None:
    with get_conn() as conn:
        conn.execute("delete from offer_items where offer_id=%s", (offer_id,))


# -----------------------------
# Lists
# -----------------------------

def list_offers(user: str, status: str | None = None):
    with get_conn() as conn:
        return conn.execute(
            """
            select
              o.id,
              o.offer_no,
              o.client_name,
              o.created_at,
              o.status,
              o.vat_rate,
              o.accepted_at,
              coalesce(sum(i.line_total), 0) as total
            from offers o
            left join offer_items i on i.offer_id = o.id
            where o.user_name = %s
              and (%s is null or o.status = %s)
            group by o.id, o.offer_no, o.client_name, o.created_at, o.status, o.vat_rate, o.accepted_at
            order by o.created_at desc, o.id desc
            """,
            (user, status, status),
        ).fetchall()


def list_clients(user: str):
    with get_conn() as conn:
        return conn.execute(
            """
            select name
            from clients
            where user_name=%s
            order by name asc
            """,
            (user,),
        ).fetchall()


def upsert_client(user: str, name: str) -> None:
    name = (name or "").strip()
    if not name:
        return
    with get_conn() as conn:
        conn.execute(
            """
            insert into clients(user_name, name)
            values (%s, %s)
            on conflict do nothing
            """,
            (user, name),
        )


# -----------------------------
# Settings
# -----------------------------

def get_settings(user: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            """
            select user_name, company_name, company_address, company_oib, company_iban,
                   company_email, company_phone, logo_path
            from company_settings
            where user_name=%s
            """,
            (user,),
        ).fetchone()
        return dict(row) if row else {}


def upsert_settings(
    user: str,
    company_name: str | None,
    company_address: str | None,
    company_oib: str | None,
    company_iban: str | None,
    company_email: str | None,
    company_phone: str | None,
    logo_path: str | None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            insert into company_settings(
              user_name, company_name, company_address, company_oib, company_iban, company_email, company_phone, logo_path
            ) values (%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (user_name) do update set
              company_name=excluded.company_name,
              company_address=excluded.company_address,
              company_oib=excluded.company_oib,
              company_iban=excluded.company_iban,
              company_email=excluded.company_email,
              company_phone=excluded.company_phone,
              logo_path=excluded.logo_path,
              updated_at=now()
            """,
            (user, company_name, company_address, company_oib, company_iban, company_email, company_phone, logo_path),
        )


# -----------------------------
# PDF / Excel exports
# -----------------------------

def _register_font(static_dir: str) -> str:
    font_path = Path(static_dir) / "DejaVuSans.ttf"
    if font_path.exists():
        try:
            pdfmetrics.registerFont(TTFont("DejaVuSans", str(font_path)))
            return "DejaVuSans"
        except Exception:
            pass
    return "Helvetica"


def render_offer_pdf(offer: Dict[str, Any], items: List[Dict[str, Any]], settings: Dict[str, Any], static_dir: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    font = _register_font(static_dir)
    c.setFont(font, 16)
    c.drawString(40, h - 50, "Ponuda")

    c.setFont(font, 10)
    if settings.get("company_name"):
        c.drawString(40, h - 70, str(settings.get("company_name")))
    if settings.get("company_address"):
        c.drawString(40, h - 85, str(settings.get("company_address")))

    c.drawRightString(w - 40, h - 70, f"Broj: {offer.get('offer_no') or ''}")
    c.drawRightString(w - 40, h - 85, f"Datum: {str(offer.get('created_at') or '')[:16]}")

    c.drawString(40, h - 110, f"Klijent: {offer.get('client_name') or ''}")

    # Table header
    y = h - 150
    c.setFont(font, 10)
    c.drawString(40, y, "Naziv")
    c.drawRightString(w - 220, y, "Količina")
    c.drawRightString(w - 140, y, "Cijena")
    c.drawRightString(w - 40, y, "Ukupno")
    y -= 10
    c.line(40, y, w - 40, y)
    y -= 16

    subtotal = Decimal("0")
    for it in items:
        name = str(it.get("name") or "")
        qty = Decimal(str(it.get("qty", 0) or 0))
        price = Decimal(str(it.get("price", 0) or 0))
        line_total = qty * price
        subtotal += line_total

        c.drawString(40, y, name[:60])
        c.drawRightString(w - 220, y, f"{qty:.2f}")
        c.drawRightString(w - 140, y, f"{price:.2f}")
        c.drawRightString(w - 40, y, f"{line_total:.2f}")
        y -= 14
        if y < 90:
            c.showPage()
            c.setFont(font, 10)
            y = h - 60

    vat_rate = Decimal(str(offer.get("vat_rate", 0) or 0))
    vat = (subtotal * vat_rate / Decimal("100")) if vat_rate else Decimal("0")
    total = subtotal + vat

    y -= 8
    c.line(40, y, w - 40, y)
    y -= 18
    c.drawRightString(w - 40, y, f"Međuzbroj: {subtotal:.2f} €")
    y -= 14
    c.drawRightString(w - 40, y, f"PDV {vat_rate:.0f}%: {vat:.2f} €")
    y -= 16
    c.setFont(font, 12)
    c.drawRightString(w - 40, y, f"Ukupno: {total:.2f} €")

    # Meta lines
    c.setFont(font, 10)
    y -= 28
    for label, key in [
        ("Mjesto", "place"),
        ("Rok isporuke", "terms_delivery"),
        ("Rok plaćanja", "terms_payment"),
        ("Napomena", "note"),
        ("Potpis", "signed_by"),
    ]:
        val = offer.get(key) or ""
        if val:
            c.drawString(40, y, f"{label}: {val}")
            y -= 14

    c.showPage()
    c.save()
    return buf.getvalue()


def render_offer_excel(offer: Dict[str, Any], items: List[Dict[str, Any]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Ponuda"

    ws.append(["Naziv", "Količina", "Cijena", "Ukupno"])
    for it in items:
        qty = float(it.get("qty", 0) or 0)
        price = float(it.get("price", 0) or 0)
        ws.append([it.get("name", ""), qty, price, qty * price])

    for col in range(1, 5):
        ws.column_dimensions[get_column_letter(col)].width = 22

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
