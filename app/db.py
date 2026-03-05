import os
from contextlib import contextmanager
import psycopg

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is required (Render provides it).")

@contextmanager
def get_conn():
    # psycopg v3 connection
    conn = psycopg.connect(DATABASE_URL, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()
