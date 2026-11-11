"""Per-user lists: one account's restaurants are invisible and untouchable to
another's, the same place can be on both lists, the list that predates
accounts goes to the first account, and alerts go to the owner's own topic."""
import sqlite3

import pytest

import app as dashboard
import auth
import db
import notifier


@pytest.fixture(autouse=True)
def _login_on(monkeypatch):
    monkeypatch.setattr(dashboard, "LOGIN_DISABLED", False)
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-key")
    monkeypatch.setenv("CHECK_NOW_COOLDOWN_HOURS", "0")


def _user(name):
    return db.create_user(name, auth.hash_password("password1"))


def _signed_in(name):
    client = dashboard.create_app().test_client()
    client.get("/login")
    with client.session_transaction() as sess:
        token = sess["csrf_token"]
    client.post("/login", data={"username": name, "password": "password1", "csrf_token": token})
    with client.session_transaction() as sess:
        sess["csrf_token"] = "t"
    return client


def _post(client, path, **data):
    return client.post(path, data={"csrf_token": "t", **data})


@pytest.fixture
def two_users():
    db.init_db()
    alice, bob = _user("alice"), _user("bob")
    db.add_restaurant("Lilia", "place-lilia", verified=True, user_id=alice)
    mine = db.get_restaurant_by_place_id("place-lilia", alice)["id"]
    return alice, bob, mine, _signed_in("alice"), _signed_in("bob")


def test_each_list_shows_only_its_owners_restaurants(two_users):
    alice, bob, _, a, b = two_users
    db.add_restaurant("Don Angie", "place-angie", verified=True, user_id=bob)
    assert b"Lilia" in a.get("/").data and b"Don Angie" not in a.get("/").data
    assert b"Don Angie" in b.get("/").data and b"Lilia" not in b.get("/").data


def test_another_users_restaurant_is_a_404(two_users):
    _, _, rid, _, b = two_users
    assert b.get(f"/restaurant/{rid}").status_code == 404
    assert b.get(f"/restaurant/{rid}/delete").status_code in (302, 404)
    for action in ("verify", "unarchive", "check", "delete", "reject"):
        assert _post(b, f"/restaurant/{rid}/{action}").status_code in (302, 404), action
    # ...and nothing of Alice's changed.
    row = db.get_restaurant(rid)
    assert row is not None and row["archived"] == 0 and row["verified_at"] is not None


def test_another_users_delete_does_not_delete(two_users):
    _, _, rid, _, b = two_users
    _post(b, f"/restaurant/{rid}/delete")
    assert db.get_restaurant(rid) is not None


def test_bulk_verify_cannot_reach_another_users_rows(two_users):
    alice, _, _, _, b = two_users
    db.add_restaurant("Unverified", "place-u", user_id=alice)
    rid = db.get_restaurant_by_place_id("place-u")["id"]
    _post(b, "/verify-selected", ids=str(rid))
    assert db.get_restaurant(rid)["verified_at"] is None


def test_the_same_place_can_be_on_two_lists():
    db.init_db()
    alice, bob = _user("alice"), _user("bob")
    db.add_restaurant("Lilia", "place-lilia", user_id=alice)
    db.add_restaurant("Lilia", "place-lilia", user_id=bob)
    db.add_restaurant("Lilia", "place-lilia", user_id=bob)  # a repeat is still ignored
    assert len(db.list_restaurants(active_only=False)) == 2
    assert len(db.list_restaurants(active_only=False, user_id=bob)) == 1
    assert (db.get_restaurant_by_place_id("place-lilia", alice)["id"]
            != db.get_restaurant_by_place_id("place-lilia", bob)["id"])


def test_adding_through_the_dashboard_files_it_under_the_signed_in_user(two_users, monkeypatch):
    alice, bob, _, _, b = two_users
    token = "stash-token"
    db.stash_pending_add(token, {"name": "Don Angie", "place_id": "place-angie",
                                 "address": "x", "maps_url": "y", "query": "Don Angie"})
    with b.session_transaction() as sess:
        sess["pending_add"] = token
    _post(b, "/add/confirm", place_id="place-angie")
    assert db.get_restaurant_by_place_id("place-angie", bob) is not None
    assert db.get_restaurant_by_place_id("place-angie", alice) is None


def test_the_first_account_adopts_the_list_that_predates_accounts():
    db.init_db()
    db.add_restaurant("Old", "place-old", verified=True)
    first, second = _user("first"), _user("second")
    assert db.get_restaurant_by_place_id("place-old")["user_id"] == first
    assert db.list_restaurants(user_id=second) == []


def test_migration_adds_owners_without_touching_existing_rows(tmp_path):
    db.init_db()
    with db.get_conn() as conn:
        conn.execute("DROP TABLE restaurants")
        conn.execute("""CREATE TABLE restaurants (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            place_id TEXT UNIQUE NOT NULL, address TEXT, maps_url TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            business_status TEXT DEFAULT 'OPERATIONAL',
            closing_soon_flag INTEGER DEFAULT 0, closing_soon_summary TEXT,
            last_checked_at TEXT, archived INTEGER DEFAULT 0, verified_at TEXT,
            pending_alert TEXT)""")
        conn.execute("DROP TABLE users")
        conn.execute("""CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("INSERT INTO restaurants (id, name, place_id) VALUES (7, 'Old', 'p')")
        conn.execute("INSERT INTO check_log (restaurant_id, business_status) VALUES (7, 'OPERATIONAL')")
        conn.execute("PRAGMA user_version = 4")

    db.init_db()

    row = db.get_restaurant(7)
    assert row["name"] == "Old" and row["user_id"] is None
    assert len(db.check_history(7)) == 1
    uid = _user("first")
    assert db.get_restaurant(7)["user_id"] == uid
    assert db.get_user(uid)["ntfy_topic"] is None
    # The place_id is no longer globally unique.
    db.add_restaurant("Old", "p", user_id=_user("second"))
    assert len(db.list_restaurants(active_only=False)) == 2


def test_deleting_an_account_removes_its_list():
    db.init_db()
    alice = _user("alice")
    db.add_restaurant("Lilia", "place-lilia", user_id=alice)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (alice,))
    assert db.list_restaurants(active_only=False) == []


# --- alerts go to the owner's topic -------------------------------------------


def _ntfy(monkeypatch):
    posts = []
    monkeypatch.setattr(notifier._session, "post", lambda url, **kw: posts.append(url))
    for var in ("SMTP_HOST", "EMAIL_FROM", "EMAIL_TO", "SMTP_USER"):
        monkeypatch.delenv(var, raising=False)
    return posts


def test_an_alert_goes_to_its_owners_topic(monkeypatch):
    db.init_db()
    alice, bob = _user("alice"), _user("bob")
    db.set_ntfy_topic(alice, "alice-private-topic")
    monkeypatch.setenv("NTFY_TOPIC", "install-default-topic")
    posts = _ntfy(monkeypatch)

    notifier.notify_closed({"name": "Lilia", "user_id": alice}, "CLOSED_PERMANENTLY")
    notifier.notify_closed({"name": "Lilia", "user_id": bob}, "CLOSED_PERMANENTLY")

    assert posts == ["https://ntfy.sh/alice-private-topic",
                     "https://ntfy.sh/install-default-topic"]


def test_a_user_without_a_topic_and_no_default_gets_no_push(monkeypatch):
    db.init_db()
    bob = _user("bob")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    posts = _ntfy(monkeypatch)
    notifier.notify_closed({"name": "Lilia", "user_id": bob}, "CLOSED_PERMANENTLY")
    assert posts == []


@pytest.mark.parametrize("topic,ok", [("a-long-random-topic-1", True), ("short", False),
                                      ("has spaces in it", False), ("x" * 65, False),
                                      ("restaurant-watcher-changeme", False),
                                      ("pick-a-unique-topic-name", False)])
def test_topic_validation(topic, ok):
    assert notifier.valid_topic(topic) is ok


def test_settings_saves_and_clears_the_topic(two_users):
    alice, _, _, a, b = two_users
    _post(a, "/settings", ntfy_topic="alice-private-topic")
    assert db.get_ntfy_topic(alice) == "alice-private-topic"
    assert b"alice-private-topic" in a.get("/settings").data
    assert b"alice-private-topic" not in b.get("/settings").data
    _post(a, "/settings", ntfy_topic="nope")
    assert db.get_ntfy_topic(alice) == "alice-private-topic"
    _post(a, "/settings", ntfy_topic="")
    assert db.get_ntfy_topic(alice) is None


def test_needs_verification_pushes_are_sent_per_user(monkeypatch):
    import main
    sent = []
    monkeypatch.setattr(main, "notify_needs_verification", sent.append)
    monkeypatch.setattr(main, "check_one", lambda *a, **k: {"news_failed": 0})
    db.init_db()
    alice, bob = _user("alice"), _user("bob")
    db.add_restaurant("A1", "pa1", user_id=alice)
    db.add_restaurant("A2", "pa2", user_id=alice)
    db.add_restaurant("B1", "pb1", user_id=bob)

    main._run_check(include_news_check=False)

    assert sorted(sorted(r["name"] for r in batch) for batch in sent) == [["A1", "A2"], ["B1"]]


def test_sqlite_is_still_usable_after_all_this():
    db.init_db()
    with sqlite3.connect(db.DB_PATH) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
