import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.getenv("DB_PATH", "ponude.db")


def _ensure_parent_dir():
    p = Path(DB_PATH)
    if p.parent and str(p.parent) not in (".", ""):
        p.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn():
    _ensure_parent_dir()
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
            CREATE TABLE IF NOT EXISTS offer_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT NOT NULL,
                name TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL NOT NULL,
                total REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_offer_items_user ON offer_items(user);")


def list_items(user: str):
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT id, name, qty, price, total FROM offer_items WHERE user=? ORDER BY id ASC",
            (user,),
        )
        return [dict(r) for r in cur.fetchall()]


def add_item(user: str, name: str, qty: float, price: float):
    total = round(qty * price, 2)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO offer_items(user, name, qty, price, total) VALUES(?,?,?,?,?)",
            (user, name, qty, price, total),
        )


def clear_items(user: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM offer_items WHERE user=?", (user,))
