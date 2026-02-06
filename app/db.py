from __future__ import annotations

import io
import os
import json
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
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


# -----------------------------
# Connection / bootstrap
# -----------------------------

def _db_url() -> str:
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
        # Users
        conn.execute(
            """
            create table if not exists users (
              id bigserial primary key,
              username text not null unique,
              created_at timestamptz not null default now()
            );
            """
        )

        # Offers (legacy user_name retained; v2 uses user_id)
        conn.execute(
            """
            create table if not exists offers (
              id bigserial primary key,
              user_id bigint,
              user_name text not null,
              client_name text,
              client_email text,
              created_at timestamptz not null default now(),
              sent_at timestamptz,

              -- numbering
              offer_year int,
              offer_seq int,
              offer_no text,

              -- workflow
              status text not null default 'DRAFT',
              accepted_at timestamptz,

              -- archive (soft delete)
              archived boolean not null default false,
              archived_at timestamptz,

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

        # Items
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

        # Company settings (legacy user_name PK retained; add user_id + logo bytes)
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

        # Clients
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

        # --- Migrations (ADD COLUMN IF NOT EXISTS) ---
        # Offers: user_id + archive fields (for older DBs)
        conn.execute("alter table offers add column if not exists user_id bigint;")
        conn.execute("alter table offers add column if not exists archived boolean not null default false;")
        conn.execute("alter table offers add column if not exists archived_at timestamptz;")

        # Offers: status workflow extras
        conn.execute("alter table offers add column if not exists client_email text;")
        conn.execute("alter table offers add column if not exists sent_at timestamptz;")

        # Settings: user_id + logo bytes
        conn.execute("alter table company_settings add column if not exists user_id bigint;")
        conn.execute("alter table company_settings add column if not exists logo_bytes bytea;")
        conn.execute("alter table company_settings add column if not exists logo_mime text;")
        conn.execute("alter table company_settings add column if not exists logo_filename text;")

        # Clients: user_id
        conn.execute("alter table clients add column if not exists user_id bigint;")

        # Indexes (keep legacy indexes too)
        conn.execute("create index if not exists idx_offers_user_name on offers(user_name);")
        conn.execute("create index if not exists idx_offers_user_id on offers(user_id);")
        conn.execute("create index if not exists idx_offer_items_offer_id on offer_items(offer_id);")
        conn.execute("create unique index if not exists idx_clients_user_name_name on clients(user_name, name);")
        conn.execute("create unique index if not exists idx_users_username on users(username);")
        conn.execute("create unique index if not exists idx_company_settings_user_id on company_settings(user_id) where user_id is not null;")
        conn.execute("create index if not exists idx_clients_user_id on clients(user_id);")
        conn.execute("create index if not exists idx_offers_user_year_seq on offers(user_name, offer_year, offer_seq);")
        conn.execute("create index if not exists idx_offers_userid_year_seq on offers(user_id, offer_year, offer_seq);")

        # Backfill users from legacy tables (if any)
        conn.execute(
            """
            insert into users(username)
            select distinct lower(user_name) from (
              select user_name from offers
              union all
              select user_name from company_settings
              union all
              select user_name from clients
            ) s
            where user_name is not null and btrim(user_name) <> ''
            on conflict do nothing;
            """
        )

        # Backfill user_id columns using users table
        conn.execute(
            """
            update offers o
            set user_id = u.id
            from users u
            where o.user_id is null
              and lower(o.user_name) = u.username;
            """
        )
        conn.execute(
            """
            update clients c
            set user_id = u.id
            from users u
            where c.user_id is null
              and lower(c.user_name) = u.username;
            """
        )
        conn.execute(
            """
            update company_settings s
            set user_id = u.id
            from users u
            where s.user_id is null
              and lower(s.user_name) = u.username;
            """
        )

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
# Users
# -----------------------------

def ensure_user(username: str) -> int:
    username = (username or "").strip().lower()
    if not username:
        raise ValueError("username required")
    with get_conn() as conn:
        row = conn.execute("select id from users where username=%s", (username,)).fetchone()
        if row:
            return int(row["id"])
        row2 = conn.execute(
            "insert into users(username) values (%s) on conflict (username) do update set username=excluded.username returning id",
            (username,),
        ).fetchone()
        return int(row2["id"])


# -----------------------------
# Offers
# -----------------------------

def _offer_owner_clause() -> str:
    # Robust for legacy rows where user_id might be null.
    return "(o.user_id = %s or (o.user_id is null and lower(o.user_name) = %s))"


def create_offer(user_id: int, username: str, client_name: str | None = None) -> int:
    year = datetime.now().year
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        row_seq = conn.execute(
            """
            select coalesce(max(offer_seq), 0) as max_seq
            from offers
            where (user_id=%s or (user_id is null and lower(user_name)=%s))
              and offer_year=%s
              and archived=false
            """,
            (user_id, username_l, year),
        ).fetchone()
        next_seq = int((row_seq or {}).get("max_seq") or 0) + 1
        offer_no = f"{year}-{next_seq:04d}"

        row = conn.execute(
            """
            insert into offers(user_id, user_name, client_name, offer_year, offer_seq, offer_no, status, vat_rate, archived)
            values (%s, %s, %s, %s, %s, %s, %s, %s, false)
            returning id
            """,
            (user_id, username_l, client_name, year, next_seq, offer_no, "DRAFT", 0),
        ).fetchone()
        return int(row["id"])


def get_offer(user_id: int, username: str, offer_id: int):
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        return conn.execute(
            f"""
            select o.id, o.user_id, o.user_name, o.client_name, o.created_at, o.offer_no, o.offer_year, o.offer_seq,
                   o.status, o.accepted_at, o.sent_at, o.archived, o.archived_at, o.client_email,
                   o.terms_delivery, o.terms_payment, o.note, o.place, o.signed_by, o.vat_rate
            from offers o
            where o.id=%s and {_offer_owner_clause()}
            """,
            (offer_id, user_id, username_l),
        ).fetchone()


def _ensure_editable(offer_row: dict) -> None:
    if not offer_row:
        return
    if offer_row.get("archived"):
        raise ValueError("Ova ponuda je arhivirana.")
    if offer_row.get("status") == "ACCEPTED":
        raise ValueError("Ova ponuda je zaključana (ACCEPTED).")



def update_offer_client_email(user_id: int, username: str, offer_id: int, client_email: str | None) -> None:
    username_l = (username or "").strip().lower()
    email = (client_email or "").strip() or None
    with get_conn() as conn:
        offer = conn.execute(
            f"select o.status, o.archived from offers o where o.id=%s and {_offer_owner_clause()}",
            (offer_id, user_id, username_l),
        ).fetchone()
        if not offer:
            return
        _ensure_editable(dict(offer))
        conn.execute("update offers set client_email=%s where id=%s", (email, offer_id))


def update_offer_client_name(user_id: int, username: str, offer_id: int, client_name: str | None) -> None:
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        offer = conn.execute(
            f"select o.status, o.archived from offers o where o.id=%s and {_offer_owner_clause()}",
            (offer_id, user_id, username_l),
        ).fetchone()
        if not offer:
            return
        _ensure_editable(dict(offer))
        conn.execute(
            "update offers set client_name=%s where id=%s",
            (client_name, offer_id),
        )


def update_offer_meta(
    user_id: int,
    username: str,
    offer_id: int,
    terms_delivery: str | None,
    terms_payment: str | None,
    note: str | None,
    place: str | None,
    signed_by: str | None,
    vat_rate: float | None,
) -> None:
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        offer = conn.execute(
            f"select o.status, o.archived from offers o where o.id=%s and {_offer_owner_clause()}",
            (offer_id, user_id, username_l),
        ).fetchone()
        if not offer:
            return
        _ensure_editable(dict(offer))
        conn.execute(
            """
            update offers
            set terms_delivery=%s,
                terms_payment=%s,
                note=%s,
                place=%s,
                signed_by=%s,
                vat_rate=%s
            where id=%s
            """,
            (terms_delivery, terms_payment, note, place, signed_by, float(vat_rate or 0), offer_id),
        )


def accept_offer(user_id: int, username: str, offer_id: int) -> None:
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        conn.execute(
            f"""
            update offers o
            set status='ACCEPTED', accepted_at=now()
            where o.id=%s and {_offer_owner_clause()} and o.archived=false
            """,
            (offer_id, user_id, username_l),
        )


def unlock_offer(user_id: int, username: str, offer_id: int) -> None:
    """
    Safety valve: allow reverting ACCEPTED -> DRAFT (keeps numbering).
    """
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        conn.execute(
            f"""
            update offers o
            set status='DRAFT', accepted_at=null
            where o.id=%s and {_offer_owner_clause()} and o.archived=false
            """,
            (offer_id, user_id, username_l),
        )



def mark_offer_sent(user_id: int, username: str, offer_id: int) -> None:
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        conn.execute(
            f"""
            update offers o
            set status='SENT', sent_at=now()
            where o.id=%s and {_offer_owner_clause()} and o.archived=false and o.status <> 'ACCEPTED'
            """,
            (offer_id, user_id, username_l),
        )


def archive_offer(user_id: int, username: str, offer_id: int) -> None:
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        conn.execute(
            f"""
            update offers o
            set archived=true, archived_at=now()
            where o.id=%s and {_offer_owner_clause()} and archived=false
            """,
            (offer_id, user_id, username_l),
        )


def unarchive_offer(user_id: int, username: str, offer_id: int) -> None:
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        conn.execute(
            f"""
            update offers o
            set archived=false, archived_at=null
            where o.id=%s and {_offer_owner_clause()} and archived=true
            """,
            (offer_id, user_id, username_l),
        )


def delete_offer_permanently(user_id: int, username: str, offer_id: int) -> None:
    """
    Hard delete allowed only if archived=true (safety).
    """
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        row = conn.execute(
            f"select o.archived from offers o where o.id=%s and {_offer_owner_clause()}",
            (offer_id, user_id, username_l),
        ).fetchone()
        if not row or not bool(row.get("archived")):
            return
        conn.execute("delete from offers where id=%s", (offer_id,))


# -----------------------------
# Items
# -----------------------------

def add_item(user_id: int, username: str, offer_id: int, name: str, qty: float, price: float) -> None:
    username_l = (username or "").strip().lower()
    q = float(qty or 0)
    p = float(price or 0)
    line_total = q * p
    with get_conn() as conn:
        offer = conn.execute(
            f"select o.status, o.archived from offers o where o.id=%s and {_offer_owner_clause()}",
            (offer_id, user_id, username_l),
        ).fetchone()
        if not offer:
            return
        _ensure_editable(dict(offer))
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


def delete_item(user_id: int, username: str, offer_id: int, item_id: int) -> None:
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        offer = conn.execute(
            f"select o.status, o.archived from offers o where o.id=%s and {_offer_owner_clause()}",
            (offer_id, user_id, username_l),
        ).fetchone()
        if not offer:
            return
        _ensure_editable(dict(offer))
        conn.execute("delete from offer_items where offer_id=%s and id=%s", (offer_id, item_id))


def clear_items(user_id: int, username: str, offer_id: int) -> None:
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        offer = conn.execute(
            f"select o.status, o.archived from offers o where o.id=%s and {_offer_owner_clause()}",
            (offer_id, user_id, username_l),
        ).fetchone()
        if not offer:
            return
        _ensure_editable(dict(offer))
        conn.execute("delete from offer_items where offer_id=%s", (offer_id,))


# -----------------------------
# Lists
# -----------------------------

def list_offers(user_id: int, username: str, show: str = "active"):
    """
    show:
      - active: archived=false
      - archived: archived=true
      - all: both
    """
    username_l = (username or "").strip().lower()
    if show not in {"active", "archived", "all"}:
        show = "active"

    archived_clause = "true"
    params: list = [user_id, username_l]

    if show == "active":
        archived_clause = "o.archived=false"
    elif show == "archived":
        archived_clause = "o.archived=true"
    else:
        archived_clause = "true"

    with get_conn() as conn:
        return conn.execute(
            f"""
            select
              o.id,
              o.offer_no,
              o.client_name,
              o.created_at,
              o.status,
              o.vat_rate,
              o.accepted_at,
              o.archived,
              o.archived_at,
              coalesce(sum(i.line_total), 0) as subtotal
            from offers o
            left join offer_items i on i.offer_id = o.id
            where ({_offer_owner_clause()})
              and {archived_clause}
            group by o.id, o.offer_no, o.client_name, o.created_at, o.status, o.vat_rate, o.accepted_at, o.archived, o.archived_at
            order by o.created_at desc, o.id desc
            """,
            (user_id, username_l),
        ).fetchall()


def list_clients(user_id: int, username: str):
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        # Prefer user_id; fallback to legacy user_name for old rows
        return conn.execute(
            """
            select name
            from clients
            where (user_id=%s or (user_id is null and lower(user_name)=%s))
            order by name asc
            """,
            (user_id, username_l),
        ).fetchall()


def upsert_client(user_id: int, username: str, name: str) -> None:
    username_l = (username or "").strip().lower()
    name = (name or "").strip()
    if not name:
        return
    with get_conn() as conn:
        conn.execute(
            """
            insert into clients(user_id, user_name, name)
            values (%s, %s, %s)
            on conflict do nothing
            """,
            (user_id, username_l, name),
        )



def dashboard_monthly(user_id: int, username: str, year: int | None = None):
    """Return monthly counts and totals (subtotal) for a given year."""
    username_l = (username or "").strip().lower()
    yr = int(year or datetime.now().year)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            select
              extract(month from o.created_at)::int as month,
              count(distinct o.id)::int as offers_count,
              coalesce(sum(i.line_total),0)::double precision as subtotal
            from offers o
            left join offer_items i on i.offer_id=o.id
            where ({_offer_owner_clause()})
              and extract(year from o.created_at)=%s
              and o.archived=false
            group by extract(month from o.created_at)
            order by month asc
            """,
            (user_id, username_l, yr),
        ).fetchall()
        # fill missing months
        bym = {int(r["month"]): {"month": int(r["month"]), "offers_count": int(r["offers_count"]), "subtotal": float(r["subtotal"])} for r in rows}
        out=[]
        for m in range(1,13):
            out.append(bym.get(m, {"month": m, "offers_count": 0, "subtotal": 0.0}))
        return out


# -----------------------------
# Settings
# -----------------------------

def get_settings(user_id: int, username: str) -> dict:
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        row = conn.execute(
            """
            select user_name, user_id, company_name, company_address, company_oib, company_iban,
                   company_email, company_phone, logo_path,
                   (logo_bytes is not null) as has_logo,
                   logo_mime, logo_filename
            from company_settings
            where (user_id=%s or (user_id is null and lower(user_name)=%s))
            """,
            (user_id, username_l),
        ).fetchone()
        return dict(row) if row else {}


def upsert_settings(
    user_id: int,
    username: str,
    company_name: str | None,
    company_address: str | None,
    company_oib: str | None,
    company_iban: str | None,
    company_email: str | None,
    company_phone: str | None,
    logo_path: str | None,
    logo_bytes: bytes | None = None,
    logo_mime: str | None = None,
    logo_filename: str | None = None,
) -> None:
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        conn.execute(
            """
            insert into company_settings(
              user_name, user_id,
              company_name, company_address, company_oib, company_iban, company_email, company_phone, logo_path,
              logo_bytes, logo_mime, logo_filename
            ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (user_name) do update set
              user_id=excluded.user_id,
              company_name=excluded.company_name,
              company_address=excluded.company_address,
              company_oib=excluded.company_oib,
              company_iban=excluded.company_iban,
              company_email=excluded.company_email,
              company_phone=excluded.company_phone,
              logo_path=excluded.logo_path,
              logo_bytes=coalesce(excluded.logo_bytes, company_settings.logo_bytes),
              logo_mime=coalesce(excluded.logo_mime, company_settings.logo_mime),
              logo_filename=coalesce(excluded.logo_filename, company_settings.logo_filename),
              updated_at=now()
            """,
            (
                username_l,
                int(user_id),
                company_name,
                company_address,
                company_oib,
                company_iban,
                company_email,
                company_phone,
                logo_path,
                psycopg.Binary(logo_bytes) if logo_bytes else None,
                logo_mime,
                logo_filename,
            ),
        )


def clear_logo(user_id: int, username: str) -> None:
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        conn.execute(
            """
            update company_settings
            set logo_bytes=null, logo_mime=null, logo_filename=null
            where (user_id=%s or (user_id is null and lower(user_name)=%s))
            """,
            (user_id, username_l),
        )


def get_logo_bytes(user_id: int, username: str) -> tuple[bytes | None, str | None]:
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        row = conn.execute(
            """
            select logo_bytes, logo_mime
            from company_settings
            where (user_id=%s or (user_id is null and lower(user_name)=%s))
            """,
            (user_id, username_l),
        ).fetchone()
        if not row:
            return None, None
        b = row.get("logo_bytes")
        # psycopg returns memoryview for bytea
        if b is not None and isinstance(b, memoryview):
            b = b.tobytes()
        return (b if b else None), (row.get("logo_mime") if row.get("logo_mime") else None)


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


def _draw_logo(c: canvas.Canvas, logo_bytes: bytes, x: float, y: float, max_w: float, max_h: float) -> float:
    """
    Draw logo with aspect ratio. Returns used height.
    """
    try:
        img = ImageReader(io.BytesIO(logo_bytes))
        iw, ih = img.getSize()
        if not iw or not ih:
            return 0.0
        scale = min(max_w / float(iw), max_h / float(ih))
        w = float(iw) * scale
        h = float(ih) * scale
        c.drawImage(img, x, y - h, width=w, height=h, mask="auto")
        return h
    except Exception:
        return 0.0


def render_offer_pdf(
    offer: Dict[str, Any],
    items: List[Dict[str, Any]],
    settings: Dict[str, Any],
    static_dir: str,
    logo_bytes: bytes | None = None,
) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    font = _register_font(static_dir)

    top_y = h - 40
    logo_h = 0.0
    if logo_bytes:
        logo_h = _draw_logo(c, logo_bytes, x=40, y=top_y, max_w=140, max_h=60)

    c.setFont(font, 16)
    c.drawString(40 + (160 if logo_h else 0), h - 50, "Ponuda")

    c.setFont(font, 10)
    y_company = h - 70
    x_company = 40 + (160 if logo_h else 0)
    if settings.get("company_name"):
        c.drawString(x_company, y_company, str(settings.get("company_name")))
        y_company -= 14
    if settings.get("company_address"):
        c.drawString(x_company, y_company, str(settings.get("company_address")))
        y_company -= 14

    c.drawRightString(w - 40, h - 70, f"Broj: {offer.get('offer_no') or ''}")
    c.drawRightString(w - 40, h - 85, f"Datum: {str(offer.get('created_at') or '')[:16]}")
    if offer.get("status") == "ACCEPTED":
        c.drawRightString(w - 40, h - 100, "Status: ACCEPTED")

    c.drawString(40, h - 120, f"Klijent: {offer.get('client_name') or ''}")

    # Table header
    y = h - 160
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



def export_user_backup_zip(user_id: int, username: str, static_dir: str) -> bytes:
    """Create a ZIP: offers.json + PDFs for all non-archived offers of the user."""
    import zipfile
    username_l = (username or "").strip().lower()
    with get_conn() as conn:
        offers = conn.execute(
            f"""
            select o.*
            from offers o
            where ({_offer_owner_clause()})
            order by o.created_at desc, o.id desc
            """,
            (user_id, username_l),
        ).fetchall()

        # collect items per offer
        offers_out = []
        items_map = {}
        for o in offers:
            oid = int(o["id"])
            items = conn.execute(
                "select id,name,qty,price,line_total from offer_items where offer_id=%s order by id asc",
                (oid,),
            ).fetchall()
            items_map[oid] = [dict(it) for it in items]
            offers_out.append(dict(o))

    # Build zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("offers.json", json.dumps({"user": username_l, "exported_at": datetime.now().isoformat(), "offers": offers_out, "items": items_map}, ensure_ascii=False, indent=2))
        # PDFs
        settings = get_settings(user_id, username_l)
        logo_bytes, _ = get_logo_bytes(user_id, username_l)
        for o in offers_out:
            oid = int(o["id"])
            pdf = render_offer_pdf(offer=o, items=items_map.get(oid, []), settings=settings, static_dir=static_dir, logo_bytes=logo_bytes)
            offer_no = (o.get("offer_no") or str(oid)).replace("/", "-")
            z.writestr(f"pdf/ponuda_{offer_no}.pdf", pdf)
    return buf.getvalue()
