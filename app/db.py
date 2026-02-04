import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


def _clean_db_url(raw: str) -> str:
    """
    Render ENV DATABASE_URL mora biti čist URL.

    Dozvoljavamo i Neon "psql '...'"
    Skidamo psql prefix, navodnike i eventualni trailing apostrof koji zna ostati.
    """
    s = (raw or "").strip()

    # Neki kopiraju cijeli snippet:  psql 'postgresql://...'
    if s.startswith("psql"):
        s = s[4:].strip()

    # Oguli višestruke početne/završne navodnike
    while len(s) >= 2 and ((s[0] == "'" and s[-1] == "'") or (s[0] == '"' and s[-1] == '"')):
        s = s[1:-1].strip()

    # Ako je ostao samo jedan navodnik
    s = s.strip().strip("'").strip('"')

    # Ako ima razmake, uzmi prvi token
    if " " in s:
        s = s.split()[0].strip().strip("'").strip('"')

    # Zadnja sigurnost: makni trailing navodnike (npr. require')
    s = s.rstrip("'").rstrip('"')

    return s


def _db_url() -> str:
    raw = os.getenv("DATABASE_URL", "")
    url = _clean_db_url(raw)

    if not url:
        raise RuntimeError("DATABASE_URL nije postavljen (Render → Environment).")

    if not (url.startswith("postgres://") or url.startswith("postgresql://")):
        raise RuntimeError("DATABASE_URL mora biti postgres/postgresql URL (bez 'psql').")

    return url


@contextmanager
def get_conn():
    # connect_timeout da deploy ne visi predugo ako DB spava
    with psycopg.connect(_db_url(), row_factory=dict_row, connect_timeout=10) as conn:
        yield conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            create table if not exists offer_items (
              id bigserial primary key,
              user_name text not null,
              name text not null,
              qty numeric(12,2) not null default 1,
              price numeric(12,2) not null default 0,
              line_total numeric(12,2) not null default 0,
              created_at timestamptz not null default now()
            );
            """
        )
        conn.execute(
            "create index if not exists idx_offer_items_user_name on offer_items(user_name);"
        )


def add_item(*, user: str, name: str, qty: float, price: float) -> None:
    line_total = float(qty) * float(price)
    with get_conn() as conn:
        conn.execute(
            """
            insert into offer_items(user_name, name, qty, price, line_total)
            values (%s, %s, %s, %s, %s)
            """,
            (user, name, qty, price, line_total),
        )


def list_items(user: str):
    with get_conn() as conn:
        return conn.execute(
            """
            select id, name, qty, price, line_total
            from offer_items
            where user_name=%s
            order by id asc
            """,
            (user,),
        ).fetchall()


def clear_items(user: str) -> None:
    with get_conn() as conn:
        conn.execute("delete from offer_items where user_name=%s", (user,))
