"""State-transition tests for db.py -- these are the easiest thing in this
repo to regress silently since main.py relies on exact field values
(business_status, closing_soon_flag, archived) to decide when to notify."""
import db


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def test_add_and_list_restaurant(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    db.add_restaurant("Lilia", "place123", address="567 Union Ave", maps_url="https://maps/x")

    restaurants = db.list_restaurants()
    assert len(restaurants) == 1
    r = restaurants[0]
    assert r["name"] == "Lilia"
    assert r["place_id"] == "place123"
    assert r["business_status"] == "OPERATIONAL"
    assert r["archived"] == 0


def test_add_restaurant_dedupes_on_place_id(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    db.add_restaurant("Lilia", "place123")
    db.add_restaurant("Lilia (dup)", "place123")

    assert len(db.list_restaurants()) == 1


def test_update_check_result_updates_state_and_logs(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    db.add_restaurant("Lilia", "place123")
    restaurant_id = db.list_restaurants()[0]["id"]

    db.update_check_result(restaurant_id, "CLOSED_PERMANENTLY", True, "Closing after 10 years")

    r = db.list_restaurants()[0]
    assert r["business_status"] == "CLOSED_PERMANENTLY"
    assert r["closing_soon_flag"] == 1
    assert r["closing_soon_summary"] == "Closing after 10 years"
    assert r["last_checked_at"] is not None

    with db.get_conn() as conn:
        log_rows = conn.execute(
            "SELECT * FROM check_log WHERE restaurant_id = ?", (restaurant_id,)
        ).fetchall()
    assert len(log_rows) == 1
    assert log_rows[0]["business_status"] == "CLOSED_PERMANENTLY"


def test_archive_restaurant_excludes_from_active_list(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    db.add_restaurant("Lilia", "place123")
    restaurant_id = db.list_restaurants()[0]["id"]

    db.archive_restaurant(restaurant_id)

    assert db.list_restaurants(active_only=True) == []
    all_restaurants = db.list_restaurants(active_only=False)
    assert len(all_restaurants) == 1
    assert all_restaurants[0]["archived"] == 1


def test_init_db_settles_a_stale_closing_soon_flag_on_a_closed_row(tmp_path, monkeypatch):
    """main.run_check() clears the flag as a permanent closure lands, but it
    can't reach rows written before it did: those archive on the run that
    closed them and are never checked again. init_db() sweeps them so the
    column means the same thing everywhere it's read."""
    _fresh_db(tmp_path, monkeypatch)
    db.add_restaurant("Lilia", "place123")
    restaurant_id = db.list_restaurants()[0]["id"]
    db.update_check_result(restaurant_id, "CLOSED_PERMANENTLY", True, "Closing after 10 years")
    db.archive_restaurant(restaurant_id)

    db.init_db()

    r = db.get_restaurant(restaurant_id)
    assert r["closing_soon_flag"] == 0
    # Only the flag is settled -- the summary is the record of what was
    # predicted, and the row stays archived.
    assert r["closing_soon_summary"] == "Closing after 10 years"
    assert r["archived"] == 1


def test_init_db_leaves_an_open_closing_soon_flag_alone(tmp_path, monkeypatch):
    """The sweep is about closures that already landed. A place still open,
    or temporarily closed, has a flag that's still saying something."""
    _fresh_db(tmp_path, monkeypatch)
    for name, place_id, status in (("Lilia", "place123", "OPERATIONAL"),
                                   ("Don Angie", "place456", "CLOSED_TEMPORARILY")):
        db.add_restaurant(name, place_id)
        row = next(r for r in db.list_restaurants() if r["name"] == name)
        db.update_check_result(row["id"], status, True, "Closing after 10 years")

    db.init_db()

    assert all(r["closing_soon_flag"] == 1 for r in db.list_restaurants())


def _log_check(restaurant_id, checked_at, business_status="OPERATIONAL", closing_soon_flag=0):
    """Append a check_log row at an explicit timestamp -- update_check_result
    always stamps CURRENT_TIMESTAMP, so aging rows has to be done directly."""
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO check_log (restaurant_id, checked_at, business_status, closing_soon_flag)
               VALUES (?, ?, ?, ?)""",
            (restaurant_id, checked_at, business_status, closing_soon_flag),
        )


def _seeded_restaurant(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    db.add_restaurant("Lilia", "place123")
    return db.list_restaurants()[0]["id"]


def _log_rows(restaurant_id):
    with db.get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM check_log WHERE restaurant_id = ? ORDER BY checked_at, id",
                (restaurant_id,),
            ).fetchall()
        ]


def _rollups(restaurant_id):
    with db.get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM check_log_monthly WHERE restaurant_id = ? ORDER BY month",
                (restaurant_id,),
            ).fetchall()
        ]


def test_prune_keeps_rows_inside_retention_window(tmp_path, monkeypatch):
    restaurant_id = _seeded_restaurant(tmp_path, monkeypatch)
    for day in ("2026-09-01 12:00:00", "2026-09-08 12:00:00", "2026-09-15 12:00:00"):
        _log_check(restaurant_id, day)

    # Retention is measured from "now", and these rows are dated in the test's
    # own recent past, so nothing should be old enough to fold away.
    assert db.prune_check_log(retain_days=36500) == 0
    assert len(_log_rows(restaurant_id)) == 3
    assert _rollups(restaurant_id) == []


def test_prune_folds_old_unchanged_rows_into_monthly_rollup(tmp_path, monkeypatch):
    restaurant_id = _seeded_restaurant(tmp_path, monkeypatch)
    for day in range(1, 5):
        _log_check(restaurant_id, f"2020-01-0{day} 12:00:00")

    assert db.prune_check_log(retain_days=0) == 3  # first row is kept as the baseline

    remaining = _log_rows(restaurant_id)
    assert [r["checked_at"] for r in remaining] == ["2020-01-01 12:00:00"]

    rollups = _rollups(restaurant_id)
    assert len(rollups) == 1
    assert rollups[0]["month"] == "2020-01"
    assert rollups[0]["checks"] == 3
    assert rollups[0]["operational_checks"] == 3
    assert rollups[0]["closed_checks"] == 0
    assert rollups[0]["first_checked_at"] == "2020-01-02 12:00:00"
    assert rollups[0]["last_checked_at"] == "2020-01-04 12:00:00"


def test_prune_keeps_status_and_flag_transitions_at_any_age(tmp_path, monkeypatch):
    restaurant_id = _seeded_restaurant(tmp_path, monkeypatch)
    _log_check(restaurant_id, "2020-01-01 12:00:00")
    _log_check(restaurant_id, "2020-01-02 12:00:00")                        # unchanged -> folded
    _log_check(restaurant_id, "2020-01-03 12:00:00", closing_soon_flag=1)   # flag flipped
    _log_check(restaurant_id, "2020-01-04 12:00:00", closing_soon_flag=1)   # unchanged -> folded
    _log_check(restaurant_id, "2020-01-05 12:00:00", "CLOSED_PERMANENTLY", 1)

    assert db.prune_check_log(retain_days=0) == 2

    assert [r["checked_at"] for r in _log_rows(restaurant_id)] == [
        "2020-01-01 12:00:00",
        "2020-01-03 12:00:00",
        "2020-01-05 12:00:00",
    ]
    assert _rollups(restaurant_id)[0]["checks"] == 2


def test_prune_is_idempotent(tmp_path, monkeypatch):
    restaurant_id = _seeded_restaurant(tmp_path, monkeypatch)
    for day in range(1, 5):
        _log_check(restaurant_id, f"2020-01-0{day} 12:00:00")

    db.prune_check_log(retain_days=0)
    before = _rollups(restaurant_id)

    # Re-pruning must not fold the retained baseline row away or double-count.
    assert db.prune_check_log(retain_days=0) == 0
    assert _rollups(restaurant_id) == before
    assert len(_log_rows(restaurant_id)) == 1


def test_prune_partitions_by_restaurant(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    db.add_restaurant("Lilia", "place123")
    db.add_restaurant("Don Angie", "place456")
    first, second = [r["id"] for r in db.list_restaurants()]

    # Interleaved in time: without PARTITION BY, each row would look like a
    # transition from the other restaurant's row and nothing would be pruned.
    _log_check(first, "2020-01-01 12:00:00")
    _log_check(second, "2020-01-01 13:00:00", "CLOSED_TEMPORARILY")
    _log_check(first, "2020-01-02 12:00:00")
    _log_check(second, "2020-01-02 13:00:00", "CLOSED_TEMPORARILY")

    assert db.prune_check_log(retain_days=0) == 2
    assert len(_log_rows(first)) == 1
    assert len(_log_rows(second)) == 1
    assert _rollups(first)[0]["operational_checks"] == 1
    assert _rollups(second)[0]["closed_checks"] == 1


def test_check_history_merges_rollups_and_live_rows(tmp_path, monkeypatch):
    restaurant_id = _seeded_restaurant(tmp_path, monkeypatch)
    for day in range(1, 5):
        _log_check(restaurant_id, f"2020-01-0{day} 12:00:00")
    db.prune_check_log(retain_days=0)
    _log_check(restaurant_id, "2020-02-01 12:00:00", "CLOSED_PERMANENTLY")

    history = db.check_history(restaurant_id)
    assert [h["month"] for h in history] == ["2020-01", "2020-02"]
    assert history[0]["checks"] == 4  # 3 folded away + the retained baseline row
    assert history[0]["operational_checks"] == 4
    assert history[1]["checks"] == 1
    assert history[1]["closed_checks"] == 1


# --- verification -------------------------------------------------------

def test_restaurants_start_unverified(tmp_path, monkeypatch):
    restaurant_id = _seeded_restaurant(tmp_path, monkeypatch)

    assert db.get_restaurant(restaurant_id)["verified_at"] is None


def test_add_restaurant_can_store_one_already_verified(tmp_path, monkeypatch):
    """What the dashboard's confirm step does -- the user already agreed to
    the match, so there's nothing left to ask."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    db.add_restaurant("Lilia", "place-lilia", verified=True)

    assert db.get_restaurant_by_place_id("place-lilia")["verified_at"] is not None


def test_verify_restaurant_is_recorded_once(tmp_path, monkeypatch):
    restaurant_id = _seeded_restaurant(tmp_path, monkeypatch)

    assert db.verify_restaurant(restaurant_id) is True
    stamped = db.get_restaurant(restaurant_id)["verified_at"]

    assert db.verify_restaurant(restaurant_id) is False
    assert db.get_restaurant(restaurant_id)["verified_at"] == stamped


def test_verify_restaurant_reports_a_missing_row(tmp_path, monkeypatch):
    _seeded_restaurant(tmp_path, monkeypatch)

    assert db.verify_restaurant(999) is False


def test_delete_restaurant_removes_its_history_too(tmp_path, monkeypatch):
    """A wrong match's check history describes some other restaurant, so it
    goes with the row rather than being left to skew a later rollup."""
    restaurant_id = _seeded_restaurant(tmp_path, monkeypatch)
    _log_check(restaurant_id, "2020-01-01 12:00:00")
    db.prune_check_log(retain_days=0)
    _log_check(restaurant_id, "2020-02-01 12:00:00")

    assert db.delete_restaurant(restaurant_id) is True

    assert db.get_restaurant(restaurant_id) is None
    assert db.check_history(restaurant_id) == []
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM check_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM check_log_monthly").fetchone()[0] == 0


def test_delete_restaurant_reports_a_missing_row(tmp_path, monkeypatch):
    _seeded_restaurant(tmp_path, monkeypatch)

    assert db.delete_restaurant(999) is False


def test_migration_verifies_rows_that_were_already_being_checked(tmp_path, monkeypatch):
    """A database written before verification existed holds restaurants the
    user has been reading alerts about for months. Treating those as
    unverified would silently stop watching all of them, so the column is
    backfilled for anything with a check against it -- and only that."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    with db.get_conn() as conn:
        # Rebuild the pre-migration table: same schema, minus the new column.
        conn.execute("DROP TABLE restaurants")
        conn.execute("""CREATE TABLE restaurants (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            place_id TEXT UNIQUE NOT NULL, address TEXT, maps_url TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            business_status TEXT DEFAULT 'OPERATIONAL',
            closing_soon_flag INTEGER DEFAULT 0, closing_soon_summary TEXT,
            last_checked_at TEXT, archived INTEGER DEFAULT 0)""")
        conn.execute("""INSERT INTO restaurants (name, place_id, last_checked_at)
                        VALUES ('Watched', 'place-watched', '2020-01-01 12:00:00')""")
        conn.execute("""INSERT INTO restaurants (name, place_id)
                        VALUES ('Seeded but never checked', 'place-fresh')""")

    db.init_db()

    assert db.get_restaurant_by_place_id("place-watched")["verified_at"] is not None
    assert db.get_restaurant_by_place_id("place-fresh")["verified_at"] is None
