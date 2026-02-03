import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL nije postavljen (Render env var)")

@contextmanager
def get_conn():
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn


# ---------- OFFERS ----------

def create_offer(user_name: str, client_name: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO offers (user_name, client_name)
            VALUES (%s, %s)
            RETURNING id
            """,
            (user_name, client_name),
        )
        return cur.fetchone()["id"]


def list_offers(user_name: str):
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT id, client_name, created_at
            FROM offers
            WHERE user_name = %s
            ORDER BY created_at DESC
            """,
            (user_name,),
        )
        return cur.fetchall()


# ---------- OFFER ITEMS ----------

def add_item(offer_id: int, name: str, qty: float, price: float):
    line_total = qty * price
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO offer_items (offer_id, name, qty, price, line_total)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (offer_id, name, qty, price, line_total),
        )


def list_items(offer_id: int):
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT id, name, qty, price, line_total
            FROM offer_items
            WHERE offer_id = %s
            ORDER BY id
            """,
            (offer_id,),
        )
        return cur.fetchall()


def clear_items(offer_id: int):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM offer_items WHERE offer_id = %s",
            (offer_id,),
        )
