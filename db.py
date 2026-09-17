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
    archived INTEGER DEFAULT 0,                    -- 1 once we've notified + user has acknowledged
    verified_at TEXT                               -- when the user confirmed this place_id is the right location; NULL until then
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

CREATE INDEX IF NOT EXISTS idx_check_log_restaurant_time
    ON check_log (restaurant_id, checked_at);

-- Rolled-up history. When detail rows in check_log age out (see
-- prune_check_log) their counts are folded in here, so how often a
-- restaurant was checked -- and what was seen -- survives without keeping
-- one row per check forever.
CREATE TABLE IF NOT EXISTS check_log_monthly (
    restaurant_id INTEGER NOT NULL,
    month TEXT NOT NULL,                           -- 'YYYY-MM'
    checks INTEGER NOT NULL DEFAULT 0,
    operational_checks INTEGER NOT NULL DEFAULT 0,
    closed_checks INTEGER NOT NULL DEFAULT 0,
    closing_soon_checks INTEGER NOT NULL DEFAULT 0,
    first_checked_at TEXT,
    last_checked_at TEXT,
    PRIMARY KEY (restaurant_id, month),
    FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
);
"""

DEFAULT_RETAIN_DAYS = 90


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


def _migrate(conn):
    """Add columns the schema gained after rows already existed.

    `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a new
    column has to be ALTERed in. Guarded by a lookup rather than a version
    number: there is one of these, and a `PRAGMA` is cheaper than a table to
    track it.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(restaurants)")}
    if "verified_at" not in columns:
        conn.execute("ALTER TABLE restaurants ADD COLUMN verified_at TEXT")
        # Backfill anything already being watched. Unverified rows are
        # skipped by run_check, so treating a restaurant that has been
        # checked for months as suddenly unverified would silently stop
        # watching it -- and there is nothing new to tell the user about a
        # place they have been reading alerts about. Never-checked rows keep
        # the NULL and get asked about, which is exactly the new behaviour.
        conn.execute("""UPDATE restaurants SET verified_at = COALESCE(added_at, CURRENT_TIMESTAMP)
                        WHERE verified_at IS NULL AND last_checked_at IS NOT NULL""")


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        # Settle any closing-soon flag left set on a permanently closed row.
        # main.run_check() clears the flag as the closure lands, but rows
        # written before it did that are unreachable by it: a permanent
        # closure archives, and archived rows are never checked again. Cheap
        # and idempotent, so it just runs every time rather than needing a
        # migration to track.
        conn.execute(
            """UPDATE restaurants SET closing_soon_flag = 0
               WHERE business_status = 'CLOSED_PERMANENTLY' AND closing_soon_flag = 1"""
        )


def add_restaurant(name, place_id, address=None, maps_url=None, verified=False):
    """Insert a restaurant, ignoring the insert if its place_id is already known.

    `verified` records that the caller already showed the user which place
    this is and got a yes -- what the dashboard's confirm step does. It
    defaults to False because the other caller, seed.py, resolves a whole
    file of names with nobody looking; those rows get asked about before
    they're ever checked (see main.run_check).
    """
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO restaurants (name, place_id, address, maps_url, verified_at)
               VALUES (?, ?, ?, ?, CASE WHEN ? THEN CURRENT_TIMESTAMP END)""",
            (name, place_id, address, maps_url, int(bool(verified))),
        )


def list_restaurants(active_only=True):
    with get_conn() as conn:
        q = "SELECT * FROM restaurants"
        if active_only:
            q += " WHERE archived = 0"
        return [dict(r) for r in conn.execute(q).fetchall()]


def get_restaurant(restaurant_id):
    """One restaurant by id, or None if there's no such row. Same dict shape
    as list_restaurants(), archived or not -- the dashboard links to archived
    rows too, and they're exactly the ones list_restaurants() hides."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM restaurants WHERE id = ?", (restaurant_id,)
        ).fetchone()
    return dict(row) if row else None


def get_restaurant_by_place_id(place_id):
    """One restaurant by its Google place_id, or None. Same dict shape as
    get_restaurant(), archived rows included.

    place_id is what add_restaurant() dedupes on, so this is the only way to
    tell "already tracking this place" from "new" -- the INSERT OR IGNORE
    itself reports neither.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM restaurants WHERE place_id = ?", (place_id,)
        ).fetchone()
    return dict(row) if row else None


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


def unarchive_restaurant(restaurant_id):
    """Put an archived restaurant back on the active list. Returns True if a
    row changed, False if there's no such restaurant.

    Only `archived` is touched. `business_status` is left alone on purpose:
    we don't know the place reopened, only that someone wants it watched
    again, and writing an optimistic OPERATIONAL here would make the next
    run read a status change and re-fire a closure alert already sent.

    Note the interaction with main.run_check(), which archives on the status
    itself rather than on the transition: if Places still reports
    CLOSED_PERMANENTLY, the next run re-archives this row (quietly -- the
    status hasn't changed, so nothing notifies). Un-archiving is for a place
    that reopened or that Google had wrong, and it sticks once the next check
    confirms that.
    """
    with get_conn() as conn:
        changed = conn.execute(
            "UPDATE restaurants SET archived = 0 WHERE id = ?", (restaurant_id,)
        ).rowcount
    return changed > 0


def verify_restaurant(restaurant_id):
    """Record that the user confirmed this row points at the right place.

    Returns True if a row was newly verified, False if it was already
    verified or doesn't exist. The timestamp is only ever written once, so a
    double-submitted form reports the repeat rather than moving the date.
    """
    with get_conn() as conn:
        changed = conn.execute(
            """UPDATE restaurants SET verified_at = CURRENT_TIMESTAMP
               WHERE id = ? AND verified_at IS NULL""",
            (restaurant_id,),
        ).rowcount
    return changed > 0


def delete_restaurant(restaurant_id):
    """Remove a restaurant and everything logged about it. Returns True if a
    row went, False if there was no such restaurant.

    This is for a row that was never the right place to begin with, which is
    why it deletes rather than archives: its check history describes some
    other restaurant, so keeping it would put wrong numbers on the page
    forever. Archiving is the tool for a place that really did close.
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM check_log WHERE restaurant_id = ?", (restaurant_id,))
        conn.execute("DELETE FROM check_log_monthly WHERE restaurant_id = ?", (restaurant_id,))
        removed = conn.execute(
            "DELETE FROM restaurants WHERE id = ?", (restaurant_id,)
        ).rowcount
    return removed > 0


def prune_check_log(retain_days=DEFAULT_RETAIN_DAYS):
    """Fold aged-out check_log rows into check_log_monthly, then delete them.

    check_log gains a row per restaurant per run, so left alone it grows
    without bound. Rows older than `retain_days` are dropped -- except the
    ones that carry signal: each restaurant's first logged check, and every
    check where business_status or closing_soon_flag differed from the check
    before it. Those are the transitions main.py notifies on, so they stay
    queryable at any age while the long runs of "same as last week" rows
    collapse into per-month counts.

    Counts in check_log_monthly cover only the folded-away rows, so a full
    month's total is that plus whatever detail rows are still in check_log.
    Only ever adding what it removes keeps repeated pruning idempotent.

    Returns the number of detail rows removed.
    """
    with get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS temp.stale_checks")
        conn.execute(
            """
            CREATE TEMP TABLE stale_checks AS
            WITH seq AS (
                SELECT id, checked_at, business_status, closing_soon_flag,
                       ROW_NUMBER() OVER w AS seq_no,
                       LAG(business_status) OVER w AS prev_status,
                       LAG(closing_soon_flag) OVER w AS prev_flag
                FROM check_log
                WINDOW w AS (PARTITION BY restaurant_id ORDER BY checked_at, id)
            )
            SELECT id FROM seq
            WHERE checked_at < datetime('now', ?)
              AND seq_no > 1
              AND business_status IS prev_status   -- IS, not =, so NULLs compare equal
              AND closing_soon_flag IS prev_flag
            """,
            (f"-{int(retain_days)} days",),
        )
        conn.execute(
            """
            INSERT INTO check_log_monthly (
                restaurant_id, month, checks, operational_checks, closed_checks,
                closing_soon_checks, first_checked_at, last_checked_at
            )
            SELECT c.restaurant_id,
                   strftime('%Y-%m', c.checked_at),
                   COUNT(*),
                   SUM(CASE WHEN c.business_status = 'OPERATIONAL' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN c.business_status LIKE 'CLOSED%' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN c.closing_soon_flag = 1 THEN 1 ELSE 0 END),
                   MIN(c.checked_at),
                   MAX(c.checked_at)
            FROM check_log c
            JOIN stale_checks s ON s.id = c.id
            GROUP BY 1, 2
            ON CONFLICT (restaurant_id, month) DO UPDATE SET
                checks = check_log_monthly.checks + excluded.checks,
                operational_checks = check_log_monthly.operational_checks
                                     + excluded.operational_checks,
                closed_checks = check_log_monthly.closed_checks + excluded.closed_checks,
                closing_soon_checks = check_log_monthly.closing_soon_checks
                                      + excluded.closing_soon_checks,
                first_checked_at = MIN(check_log_monthly.first_checked_at,
                                       excluded.first_checked_at),
                last_checked_at = MAX(check_log_monthly.last_checked_at,
                                      excluded.last_checked_at)
            """
        )
        removed = conn.execute(
            "DELETE FROM check_log WHERE id IN (SELECT id FROM stale_checks)"
        ).rowcount
        conn.execute("DROP TABLE temp.stale_checks")
    return removed


def check_history(restaurant_id):
    """Per-month check history for one restaurant: rolled-up counts plus the
    detail rows still in check_log, merged into one row per month."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT month,
                   SUM(checks) AS checks,
                   SUM(operational_checks) AS operational_checks,
                   SUM(closed_checks) AS closed_checks,
                   SUM(closing_soon_checks) AS closing_soon_checks,
                   MIN(first_checked_at) AS first_checked_at,
                   MAX(last_checked_at) AS last_checked_at
            FROM (
                SELECT month, checks, operational_checks, closed_checks,
                       closing_soon_checks, first_checked_at, last_checked_at
                FROM check_log_monthly
                WHERE restaurant_id = ?
                UNION ALL
                SELECT strftime('%Y-%m', checked_at),
                       COUNT(*),
                       SUM(CASE WHEN business_status = 'OPERATIONAL' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN business_status LIKE 'CLOSED%' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN closing_soon_flag = 1 THEN 1 ELSE 0 END),
                       MIN(checked_at),
                       MAX(checked_at)
                FROM check_log
                WHERE restaurant_id = ?
                GROUP BY 1
            )
            GROUP BY month
            ORDER BY month
            """,
            (restaurant_id, restaurant_id),
        ).fetchall()
    return [dict(r) for r in rows]
