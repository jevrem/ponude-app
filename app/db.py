import os
from contextlib import contextmanager
from datetime import datetime

import psycopg
from psycopg.rows import dict_row


def _db_url() -> str:
    # Render/Neon typically provide DATABASE_URL
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or ""
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


@contextmanager
def get_conn():
    with psycopg.connect(_db_url(), row_factory=dict_row) as conn:
        yield conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            create table if not exists offers (
              id bigserial primary key,
              user_name text not null,
              client_name text null,
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
              qty double precision not null,
              price double precision not null,
              line_total double precision not null,
              created_at timestamptz not null default now()
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

        # Add numbering columns (safe to run multiple times)
        conn.execute("alter table offers add column if not exists offer_year int;")
        conn.execute("alter table offers add column if not exists offer_seq int;")
        conn.execute("alter table offers add column if not exists offer_no text;")
        conn.execute("create index if not exists idx_offers_user_year_seq on offers(user_name, offer_year, offer_seq);")

        # Add status column
        conn.execute("alter table offers add column if not exists status text;")
        conn.execute("update offers set status='DRAFT' where status is null;")
        try:
            conn.execute("alter table offers alter column status set default 'DRAFT';")
        except Exception:
            pass
        try:
            conn.execute("alter table offers alter column status set not null;")
        except Exception:
            pass


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
        next_seq = int(row_seq["max_seq"] or 0) + 1
        offer_no = f"{year}-{next_seq:04d}"

        row = conn.execute(
            """
            insert into offers(user_name, client_name, offer_year, offer_seq, offer_no, status)
            values (%s, %s, %s, %s, %s, %s)
            returning id
            """,
            (user, client_name, year, next_seq, offer_no, "DRAFT"),
        ).fetchone()
        return int(row["id"])


def get_offer(user: str, offer_id: int):
    with get_conn() as conn:
        return conn.execute(
            """
            select id, user_name, client_name, created_at, offer_no, offer_year, offer_seq, status
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


def add_item(offer_id: int, name: str, qty: float, price: float) -> None:
    line_total = float(qty) * float(price)
    with get_conn() as conn:
        conn.execute(
            """
            insert into offer_items(offer_id, name, qty, price, line_total)
            values (%s, %s, %s, %s, %s)
            """,
            (offer_id, name, float(qty), float(price), float(line_total)),
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
        conn.execute("delete from offer_items where offer_id=%s and id=%s", (offer_id, item_id))


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
              coalesce(sum(i.line_total), 0) as total
            from offers o
            left join offer_items i on i.offer_id = o.id
            where o.user_name = %s
              and (%s is null or o.status = %s)
            group by o.id, o.offer_no, o.client_name, o.created_at, o.status
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
