from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row


def _db_url() -> str:
    # Render/Neon usually provide DATABASE_URL.
    # If you also keep NEON_DATABASE_URL, we accept that too.
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("NEON_DATABASE_URL")
        or os.environ.get("POSTGRES_URL")
        or ""
    )


@contextmanager
def get_conn():
    url = _db_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    with psycopg.connect(url, row_factory=dict_row) as conn:
        yield conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            create table if not exists offers (
              id bigserial primary key,
              user_name text not null,
              offer_no text not null,
              created_at timestamptz not null default now(),
              status text not null default 'DRAFT',
              client_name text,
              place text,
              signed_by text,
              delivery_term text,
              payment_term text,
              note text,
              vat_rate int not null default 0
            );
            """
        )
        conn.execute(
            """
            create table if not exists offer_items (
              id bigserial primary key,
              offer_id bigint not null references offers(id) on delete cascade,
              name text not null,
              qty numeric not null default 1,
              price numeric not null default 0
            );
            """
        )
        conn.execute("create index if not exists idx_offers_user on offers(user_name);")
        conn.execute("create index if not exists idx_offer_items_offer on offer_items(offer_id);")

        conn.execute(
            """
            create table if not exists settings (
              user_name text primary key,
              company_name text,
              company_address text,
              company_oib text,
              company_iban text,
              company_email text,
              company_phone text,
              logo_path text
            );
            """
        )
        conn.commit()


def _next_offer_no(conn, user: str) -> str:
    # Format: YYYY-0001 per user
    year = datetime.now().strftime("%Y")
    row = conn.execute(
        "select offer_no from offers where user_name=%s and offer_no like %s order by id desc limit 1",
        (user, f"{year}-%",),
    ).fetchone()
    if not row:
        seq = 1
    else:
        try:
            seq = int(str(row["offer_no"]).split("-")[1]) + 1
        except Exception:
            seq = 1
    return f"{year}-{seq:04d}"


def create_offer(user: str) -> int:
    user = (user or "").strip().lower()
    with get_conn() as conn:
        offer_no = _next_offer_no(conn, user)
        row = conn.execute(
            "insert into offers(user_name, offer_no) values (%s,%s) returning id",
            (user, offer_no),
        ).fetchone()
        conn.commit()
        return int(row["id"])


def ensure_active_offer(user: str) -> int:
    user = (user or "").strip().lower()
    with get_conn() as conn:
        row = conn.execute(
            "select id from offers where user_name=%s order by id desc limit 1",
            (user,),
        ).fetchone()
        if row:
            return int(row["id"])
        offer_no = _next_offer_no(conn, user)
        row = conn.execute(
            "insert into offers(user_name, offer_no) values (%s,%s) returning id",
            (user, offer_no),
        ).fetchone()
        conn.commit()
        return int(row["id"])


def get_offer(offer_id: int) -> Dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("select * from offers where id=%s", (offer_id,)).fetchone()
        return dict(row) if row else {}


def list_offers(user: str) -> List[Dict[str, Any]]:
    user = (user or "").strip().lower()
    with get_conn() as conn:
        rows = conn.execute(
            "select * from offers where user_name=%s order by id desc limit 200",
            (user,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_items(offer_id: int) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "select id, offer_id, name, qty, price from offer_items where offer_id=%s order by id asc",
            (offer_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def add_item(offer_id: int, name: str, qty: Decimal, price: Decimal) -> None:
    name = (name or "").strip()
    if not name:
        return
    with get_conn() as conn:
        conn.execute(
            "insert into offer_items(offer_id, name, qty, price) values (%s,%s,%s,%s)",
            (offer_id, name, qty, price),
        )
        conn.commit()


def delete_item(offer_id: int, item_id: int) -> None:
    with get_conn() as conn:
        conn.execute("delete from offer_items where offer_id=%s and id=%s", (offer_id, item_id))
        conn.commit()


def clear_items(offer_id: int) -> None:
    with get_conn() as conn:
        conn.execute("delete from offer_items where offer_id=%s", (offer_id,))
        conn.commit()


def set_client_name(offer_id: int, client_name: Optional[str]) -> None:
    with get_conn() as conn:
        conn.execute("update offers set client_name=%s where id=%s", (client_name, offer_id))
        conn.commit()


def set_status(offer_id: int, status: str) -> None:
    status = (status or "DRAFT").strip().upper()
    if status not in {"DRAFT", "SENT", "ACCEPTED"}:
        status = "DRAFT"
    with get_conn() as conn:
        conn.execute("update offers set status=%s where id=%s", (status, offer_id))
        conn.commit()


def set_meta(
    offer_id: int,
    place: Optional[str],
    signed_by: Optional[str],
    delivery_term: Optional[str],
    payment_term: Optional[str],
    note: Optional[str],
    vat_rate: int,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            update offers
            set place=%s, signed_by=%s, delivery_term=%s, payment_term=%s, note=%s, vat_rate=%s
            where id=%s
            """,
            (place, signed_by, delivery_term, payment_term, note, int(vat_rate or 0), offer_id),
        )
        conn.commit()


def compute_totals(items: List[Dict[str, Any]], vat_rate: Decimal) -> Dict[str, Any]:
    subtotal = Decimal("0")
    for it in items:
        q = Decimal(str(it.get("qty") or 0))
        p = Decimal(str(it.get("price") or 0))
        subtotal += q * p
    vat = (subtotal * (Decimal(vat_rate) / Decimal("100"))).quantize(Decimal("0.01")) if vat_rate else Decimal("0.00")
    total = (subtotal + vat).quantize(Decimal("0.01"))
    return {
        "subtotal": subtotal.quantize(Decimal("0.01")),
        "vat_rate": int(vat_rate or 0),
        "vat": vat,
        "total": total,
    }


def get_settings(user: str) -> Dict[str, Any]:
    user = (user or "").strip().lower()
    with get_conn() as conn:
        row = conn.execute("select * from settings where user_name=%s", (user,)).fetchone()
        return dict(row) if row else {}


def save_settings(
    user: str,
    company_name: Optional[str],
    company_address: Optional[str],
    company_oib: Optional[str],
    company_iban: Optional[str],
    company_email: Optional[str],
    company_phone: Optional[str],
    logo_path: Optional[str],
) -> None:
    user = (user or "").strip().lower()
    with get_conn() as conn:
        conn.execute(
            """
            insert into settings(user_name, company_name, company_address, company_oib, company_iban, company_email, company_phone, logo_path)
            values (%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (user_name) do update set
              company_name=excluded.company_name,
              company_address=excluded.company_address,
              company_oib=excluded.company_oib,
              company_iban=excluded.company_iban,
              company_email=excluded.company_email,
              company_phone=excluded.company_phone,
              logo_path=excluded.logo_path
            """,
            (user, company_name, company_address, company_oib, company_iban, company_email, company_phone, logo_path),
        )
        conn.commit()
