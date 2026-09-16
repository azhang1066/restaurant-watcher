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
