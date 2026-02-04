import os
from contextlib import contextmanager
from datetime import datetime

import psycopg
from psycopg.rows import dict_row


def _normalize_db_url(raw: str) -> str:
    """Normalize DATABASE_URL.

    Accepts either a plain postgres URL or a Neon 'psql' snippet like:
        psql 'postgres://user:pass@host/db?sslmode=require'

    Returns a clean URL suitable for psycopg.connect().
    """
    s = (raw or "").strip()

    # If user pasted the Neon CLI snippet, it often starts with: psql '...'
    if s.startswith("psql"):
        s = s[4:].strip()

    # Strip wrapping quotes
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        s = s[1:-1].strip()

    # If there are still spaces, keep only first token (the URL itself has no spaces)
    if " " in s:
        s = s.split()[0].strip()
        if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
            s = s[1:-1].strip()

    return s


def _db_url() -> str:
    raw = os.getenv("DATABASE_URL", "").strip()
    if not raw:
        raise RuntimeError("DATABASE_URL nije postavljen (Render Environment var).")

    url = _normalize_db_url(raw)
    if not (url.startswith("postgres://") or url.startswith("postgresql://")):
        raise RuntimeError(
            "DATABASE_URL izgleda krivo. Zalijepi samo postgres URL (bez 'psql')."
        )
    return url


@contextmanager
def get_conn():
    with psycopg.connect(_db_url(), row_factory=dict_row) as conn:
        yield conn


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            create table if not exists offers (
              id bigserial primary key,
              user_name text not null,
              client_name text,
              created_at timestamptz not null default now()
            );
            """
        )
        # Add numbering columns (safe to run multiple times)
        conn.execute("alter table offers add column if not exists offer_year int;")
        conn.execute("alter table offers add column if not exists offer_seq int;")
        conn.execute("alter table offers add column if not exists offer_no text;")
        conn.execute(
            """
            create table if not exists offer_items (
              id bigserial primary key,
              offer_id bigint not null references offers(id) on delete cascade,
              name text not null,
              qty numeric(12,2) not null default 1,
              price numeric(12,2) not null default 0,
              line_total numeric(12,2) not null default 0
            );
            """
        )
        conn.execute("create index if not exists idx_offers_user_name on offers(user_name);")
        conn.execute("create index if not exists idx_offers_user_year_seq on offers(user_name, offer_year, offer_seq);")
        conn.execute("create index if not exists idx_offer_items_offer_id def create_offer(user: str, client_name: str | None = None) -> int:
    year = datetime.now().year
    with get_conn() as conn:
        # Next sequence per user per year
        row_seq = conn.execute(
            """
            select coalesce(max(offer_seq), 0) as max_seq
            from offers
            where user_name=%s and offer_year=%s
            """,
            (user, year),
        ).fetchone()
        next_seq = int(row_seq["max_seq"] or 0) + 1
        offer_no = f"{year}-{next_seq:04d}"

        row = conn.execute(
            """
            insert into offers(user_name, client_name, offer_year, offer_seq, offer_no)
            values (%s, %s, %s, %s, %s)
            returning id
            """,
            (user, client_name, year, next_seq, offer_no),
        ).fetchone()
        return int(row["id"])
        return int(row["id"])


def add_item(offer_id: int, name: str, qty: float, price: float) -> None:
    line_total = float(qty) * float(price)
    with get_conn() as conn:
        conn.execute(
            """
            insert into offer_items(offer_id, name, qty, price, line_total)
            values (%s, %s, %s, %s, %s)
            """,
            (offer_id, name, qty, price, line_total),
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


def clear_items(offer_id: int) -> None:
    with get_conn() as conn:
        conn.execute("delete from offer_items where offer_id=%s", (offer_id,))
