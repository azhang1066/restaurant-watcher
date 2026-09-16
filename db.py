"""SQLite storage for tracked restaurants and their check history."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "restaurants.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS restaurants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    place_id TEXT UNIQUE NOT NULL,
    address TEXT,
    maps_url TEXT,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP,
    business_status TEXT DEFAULT 'OPERATIONAL',   -- OPERATIONAL / CLOSED_TEMPORARILY / CLOSED_PERMANENTLY
    closing_soon_flag INTEGER DEFAULT 0,           -- 1 if news check found a closure signal
    closing_soon_summary TEXT,
    last_checked_at TEXT,
    archived INTEGER DEFAULT 0                     -- 1 once we've notified + user has acknowledged
);

CREATE TABLE IF NOT EXISTS check_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL,
    checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
    business_status TEXT,
    closing_soon_flag INTEGER,
    notes TEXT,
    FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
);
"""


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def add_restaurant(name, place_id, address=None, maps_url=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO restaurants (name, place_id, address, maps_url)
               VALUES (?, ?, ?, ?)""",
            (name, place_id, address, maps_url),
        )


def list_restaurants(active_only=True):
    with get_conn() as conn:
        q = "SELECT * FROM restaurants"
        if active_only:
            q += " WHERE archived = 0"
        return [dict(r) for r in conn.execute(q).fetchall()]


def update_check_result(restaurant_id, business_status, closing_soon_flag, closing_soon_summary, notes=""):
    with get_conn() as conn:
        conn.execute(
            """UPDATE restaurants
               SET business_status = ?, closing_soon_flag = ?, closing_soon_summary = ?,
                   last_checked_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (business_status, int(closing_soon_flag), closing_soon_summary, restaurant_id),
        )
        conn.execute(
            """INSERT INTO check_log (restaurant_id, business_status, closing_soon_flag, notes)
               VALUES (?, ?, ?, ?)""",
            (restaurant_id, business_status, int(closing_soon_flag), notes),
        )


def archive_restaurant(restaurant_id):
    with get_conn() as conn:
        conn.execute("UPDATE restaurants SET archived = 1 WHERE id = ?", (restaurant_id,))
