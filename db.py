"""SQLite storage for tracked restaurants and their check history."""
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from statuses import (ALL_STATUSES, CLOSED_PERMANENTLY, CLOSED_STATUSES,
                      OPERATIONAL)

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "restaurants.db"

_STATUS_LIST = ", ".join(f"'{s}'" for s in ALL_STATUSES)
_CLOSED_LIST = ", ".join(f"'{s}'" for s in CLOSED_STATUSES)

# The restaurants table as of the latest schema version. Also what migration 2
# rebuilds an older table into, so there is one definition of it, not two.
_RESTAURANTS_COLUMNS = f"""
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    place_id TEXT UNIQUE NOT NULL,
    address TEXT,
    maps_url TEXT,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP,
    business_status TEXT DEFAULT '{OPERATIONAL}'
        CHECK (business_status IN ({_STATUS_LIST})),
    closing_soon_flag INTEGER DEFAULT 0 CHECK (closing_soon_flag IN (0, 1)),   -- 1 if news check found a closure signal
    closing_soon_summary TEXT,
    last_checked_at TEXT,
    archived INTEGER DEFAULT 0 CHECK (archived IN (0, 1)),                     -- 1 once we've notified + user has acknowledged
    verified_at TEXT                               -- when the user confirmed this place_id is the right location; NULL until then
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS restaurants ({_RESTAURANTS_COLUMNS});

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

# Created after migrations rather than in SCHEMA: migration 2 drops and
# recreates the restaurants table, which takes its indexes with it.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_restaurants_archived ON restaurants (archived);
"""

DEFAULT_RETAIN_DAYS = 90
DEFAULT_BACKUPS_KEPT = 8

# The dashboard and the scheduler are separate processes sharing one file.
# Wait this long for the other's write lock instead of failing at once with
# "database is locked" (sqlite3's default is 5 seconds, which a prune can exceed).
BUSY_TIMEOUT_SECONDS = 30


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    # WAL lets the dashboard read while the scheduler writes. The mode is
    # stored in the database file, so this is a no-op after the first time.
    conn.execute("PRAGMA journal_mode = WAL")
    # Off by default and per-connection: without it the REFERENCES clauses in
    # SCHEMA are documentation, not constraints.
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_1_add_verified_at(conn):
    """Add `verified_at` to a table that predates it, backfilling anything
    already being watched.

    Unverified rows are skipped by run_check, so treating a restaurant that
    has been checked for months as unverified would silently stop watching
    it. Never-checked rows keep the NULL and get asked about.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(restaurants)")}
    if "verified_at" in columns:
        return
    conn.execute("ALTER TABLE restaurants ADD COLUMN verified_at TEXT")
    conn.execute("""UPDATE restaurants SET verified_at = COALESCE(added_at, CURRENT_TIMESTAMP)
                    WHERE verified_at IS NULL AND last_checked_at IS NOT NULL""")


def _migrate_2_add_constraints(conn):
    """Rebuild `restaurants` so status and flag columns carry CHECK constraints.

    SQLite can't add a CHECK to an existing table, so this is the documented
    create-copy-drop-rename. Row ids are copied across, so `check_log` and
    `check_log_monthly` keep pointing at the right rows. A row that violates
    a constraint aborts the whole migration (nothing is committed) rather
    than being quietly dropped or rewritten.
    """
    names = [row["name"] for row in conn.execute("PRAGMA table_info(restaurants)")]
    cols = ", ".join(names)
    conn.execute(f"CREATE TABLE restaurants_new ({_RESTAURANTS_COLUMNS})")
    conn.execute(f"INSERT INTO restaurants_new ({cols}) SELECT {cols} FROM restaurants")
    conn.execute("DROP TABLE restaurants")
    conn.execute("ALTER TABLE restaurants_new RENAME TO restaurants")


# Schema version N lives in `PRAGMA user_version`; _MIGRATIONS[N-1] takes a
# database from version N-1 to N. Append to the list to add one -- each runs
# once, in order, inside a transaction.
_MIGRATIONS = [_migrate_1_add_verified_at, _migrate_2_add_constraints]
SCHEMA_VERSION = len(_MIGRATIONS)


def _migrate(conn):
    """Bring an existing database up to SCHEMA_VERSION."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    # Rebuilding `restaurants` (migration 2) drops a table the log tables
    # reference, which enforcement would refuse. The pragma is a no-op inside
    # a transaction, so it is switched off here, before the first BEGIN, and
    # back on once the last migration has committed.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for number in range(version + 1, SCHEMA_VERSION + 1):
            conn.execute("BEGIN")
            try:
                _MIGRATIONS[number - 1](conn)
                # PRAGMA doesn't take parameters; `number` is an int we control.
                conn.execute(f"PRAGMA user_version = {number}")
            except BaseException:
                # Explicit rather than left to conn.close(): the half-applied
                # migration must not be committed by get_conn's own commit.
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
        if orphans:
            # Rows that predate enforcement. Reported, not fixed: guessing
            # which side to delete is the user's call.
            logger.warning("%d row(s) reference a restaurant that doesn't exist "
                           "(PRAGMA foreign_key_check).", len(orphans))
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def init_db():
    with get_conn() as conn:
        fresh = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'restaurants'"
        ).fetchone() is None
        conn.executescript(SCHEMA)
        if fresh:
            # SCHEMA is already the latest shape; nothing to migrate.
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        else:
            _migrate(conn)
        conn.executescript(INDEXES)
        # Settle any closing-soon flag left set on a permanently closed row.
        # checker.check_one() clears the flag as the closure lands, but a row
        # written by anything else is unreachable by it: a permanent closure
        # archives, and archived rows are never checked again. Cheap and
        # idempotent, so it just runs every time.
        conn.execute(
            """UPDATE restaurants SET closing_soon_flag = 0
               WHERE business_status = ? AND closing_soon_flag = 1""",
            (CLOSED_PERMANENTLY,),
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


def update_check_result(restaurant_id, business_status, closing_soon_flag,
                        closing_soon_summary, archive=False):
    """Record one check: the row's new state plus its check_log entry.

    `archive` flips the archived flag in the same transaction, so a crash
    can't leave a permanently closed row recorded but still on the active list.
    """
    with get_conn() as conn:
        conn.execute(
            """UPDATE restaurants
               SET business_status = ?, closing_soon_flag = ?, closing_soon_summary = ?,
                   last_checked_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (business_status, int(closing_soon_flag), closing_soon_summary, restaurant_id),
        )
        conn.execute(
            """INSERT INTO check_log (restaurant_id, business_status, closing_soon_flag)
               VALUES (?, ?, ?)""",
            (restaurant_id, business_status, int(closing_soon_flag)),
        )
        if archive:
            conn.execute("UPDATE restaurants SET archived = 1 WHERE id = ?", (restaurant_id,))


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

    Note the interaction with checker.check_one(), which archives on the status
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


def verify_restaurants(restaurant_ids):
    """Record that the user confirmed these rows point at the right place, in
    one transaction. Returns how many were newly verified.

    The timestamp is only ever written once, so ids that are unknown or
    already verified are skipped -- a double-submitted form reports the repeat
    rather than moving the date.
    """
    ids = [(i,) for i in restaurant_ids]
    if not ids:
        return 0
    with get_conn() as conn:
        return conn.executemany(
            """UPDATE restaurants SET verified_at = CURRENT_TIMESTAMP
               WHERE id = ? AND verified_at IS NULL""",
            ids,
        ).rowcount


def verify_restaurant(restaurant_id):
    """verify_restaurants() for one row: True if it was newly verified, False
    if it was already verified or doesn't exist."""
    return verify_restaurants([restaurant_id]) > 0


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
    before it. Those are the transitions checker.check_one notifies on, so they stay
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
            f"""
            INSERT INTO check_log_monthly (
                restaurant_id, month, checks, operational_checks, closed_checks,
                closing_soon_checks, first_checked_at, last_checked_at
            )
            SELECT c.restaurant_id,
                   strftime('%Y-%m', c.checked_at),
                   COUNT(*),
                   SUM(CASE WHEN c.business_status = '{OPERATIONAL}' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN c.business_status IN ({_CLOSED_LIST}) THEN 1 ELSE 0 END),
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
            f"""
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
                       SUM(CASE WHEN business_status = '{OPERATIONAL}' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN business_status IN ({_CLOSED_LIST}) THEN 1 ELSE 0 END),
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


def backup_db(keep=DEFAULT_BACKUPS_KEPT):
    """Copy the database to a timestamped file in `backups/` beside it, then
    delete all but the newest `keep`. Returns the new file's path.

    The dashboard's verify queue is hand-done work with no other copy, and
    `data/` is gitignored. Uses SQLite's online backup API, which is safe
    while the dashboard has the file open (a plain file copy isn't in WAL
    mode).
    """
    init_db()
    folder = DB_PATH.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = folder / f"{DB_PATH.stem}-{stamp}.db"
    with get_conn() as source:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    # Names sort chronologically, so the oldest are the head of the list.
    old = sorted(folder.glob(f"{DB_PATH.stem}-*.db"))[:-keep] if keep > 0 else []
    for path in old:
        path.unlink()
    return target
