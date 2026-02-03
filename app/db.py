# app/db.py
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "data/ponude.db")


def _ensure_dir():
    d = os.path.dirname(DB_PATH)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


@contextmanager
def get_conn():
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS offer_item (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL,
                name TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def list_items(user: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, user, name, qty, price FROM offer_item WHERE user = ? ORDER BY id ASC",
            (user,),
        ).fetchall()
        return [dict(r) for r in rows]


def add_item(user: str, name: str, qty: float, price: float):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO offer_item (user, name, qty, price) VALUES (?, ?, ?, ?)",
            (user, name, qty, price),
        )


def clear_items(user: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM offer_item WHERE user = ?", (user,))

