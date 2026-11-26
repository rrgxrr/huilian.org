"""SQLite access helpers."""
import sqlite3
from pathlib import Path

from flask import current_app, g


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"], detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db() -> None:
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    get_db().executescript(schema)
    get_db().commit()


def query(sql: str, args=(), one: bool = False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql: str, args=()) -> int:
    """Run a write and commit. Returns lastrowid."""
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid
