import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

# Set DATABASE_URL in Render (Neon connection string)
DATABASE_URL = os.getenv("DATABASE_URL")


def _db_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set in environment variables.")
    return DATABASE_URL


@contextmanager
def get_conn():
    with psycopg.connect(_db_url(), row_factory=dict_row) as conn:
        yield conn


def init_db() -> None:
    """Create minimal tables if they don't exist."""
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS offer_items (
                id BIGSERIAL PRIMARY KEY,
                user_name TEXT NOT NULL,
                name TEXT NOT NULL,
                qty NUMERIC(12,2) NOT NULL DEFAULT 1,
                price NUMERIC(12,2) NOT NULL DEFAULT 0,
                line_total NUMERIC(12,2) NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_offer_items_user_name
            ON offer_items(user_name);
            """
        )


def list_items(user: str):
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT id, name, qty, price, line_total
            FROM offer_items
            WHERE user_name = %s
            ORDER BY id ASC
            """,
            (user,),
        )
        return cur.fetchall()


def add_item(user: str, name: str, qty: float, price: float):
    line_total = round(float(qty) * float(price), 2)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO offer_items (user_name, name, qty, price, line_total)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (user, name, qty, price, line_total),
        )


def clear_items(user: str):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM offer_items WHERE user_name = %s",
            (user,),
        )
