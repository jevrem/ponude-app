import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


def _strip_wrapping_quotes(s: str) -> str:
    # Robustly remove accidental leading/trailing quotes, even if only one side is present.
    s = (s or "").strip()
    while s and s[0] in ("'", '"'):
        s = s[1:].lstrip()
    while s and s[-1] in ("'", '"'):
        s = s[:-1].rstrip()
    return s


def _normalize_db_url(raw: str) -> str:
    """Normalize DATABASE_URL.

    Accepts either a plain postgres URL or a Neon 'psql' snippet like:
        psql 'postgres://user:pass@host/db?sslmode=require'

    Returns a clean URL suitable for psycopg.connect().
    """
    s = (raw or "").strip()

    # If user pasted the Neon CLI snippet, it often starts with: psql '...'
    if s.lower().startswith("psql"):
        # Drop the leading "psql" token and keep the rest
        parts = s.split(None, 1)
        s = parts[1].strip() if len(parts) > 1 else ""

    s = _strip_wrapping_quotes(s)

    # Sometimes env var contains extra tokens; keep only the actual URL.
    if "postgres://" in s:
        s = s[s.find("postgres://") :].strip()
    elif "postgresql://" in s:
        s = s[s.find("postgresql://") :].strip()

    # If there are still spaces, keep only first token (a real URL has no spaces)
    if " " in s:
        s = s.split()[0].strip()

    s = _strip_wrapping_quotes(s)

    # Remove an accidental trailing semicolon if copied from somewhere
    if s.endswith(";"):
        s = s[:-1].strip()

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

        # Indexes (NOTE: offer_items has no user_name; we index by offer_id instead)
        conn.execute("create index if not exists idx_offers_user_name on offers(user_name);")
        conn.execute("create index if not exists idx_offer_items_offer_id on offer_items(offer_id);")


def create_offer(user: str, client_name: str | None = None) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "insert into offers(user_name, client_name) values (%s, %s) returning id",
            (user, client_name),
        ).fetchone()
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



def delete_item(offer_id: int, item_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "delete from offer_items where offer_id=%s and id=%s",
            (offer_id, item_id),
        )
