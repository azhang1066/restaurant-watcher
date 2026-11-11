"""Tests for accounts, login, sign-up and the rate limits (auth.py, and their
wiring in app.py). Who sees which restaurants is in test_user_lists.py."""
import pytest

import app as dashboard
import auth
import db


@pytest.fixture(autouse=True)
def _login_on(monkeypatch):
    monkeypatch.setattr(dashboard, "LOGIN_DISABLED", False)
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-key")
    monkeypatch.setenv("CHECK_NOW_COOLDOWN_HOURS", "0")


def _new_client():
    return dashboard.create_app().test_client()


def _make_user(username="admin", password="hunter2"):
    db.init_db()
    return db.create_user(username, auth.hash_password(password))


@pytest.fixture
def locked():
    _make_user()
    return _new_client()


def _login(client, password="hunter2", username="admin"):
    client.get("/login")  # mints the CSRF token
    with client.session_transaction() as sess:
        token = sess["csrf_token"]
    return client.post("/login", data={"username": username, "password": password,
                                       "csrf_token": token})


def test_nobody_gets_in_when_there_are_no_accounts():
    client = _new_client()
    assert client.get("/").status_code == 302
    page = client.get("/login")
    assert page.status_code == 200 and b"Create an account" in page.data
    assert _login(client, "").headers["Location"].endswith("/login")


def test_an_account_works_from_any_device():
    _make_user("andrew", "correct horse")
    first, second, third = (_new_client() for _ in range(3))
    assert _login(first, "correct horse", "andrew").headers["Location"].endswith("/")
    assert _login(second, "correct horse", "Andrew").headers["Location"].endswith("/")
    assert _login(third, "wrong", "andrew").headers["Location"].endswith("/login")
    assert _login(_new_client(), "correct horse", "admin").headers["Location"].endswith("/login")


def test_pages_redirect_to_login_when_signed_out(locked):
    for path in ("/", "/restaurant/1", "/add/confirm", "/restaurant/1/delete", "/settings"):
        resp = locked.get(path)
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")


def test_signed_out_post_is_redirected_not_executed(locked):
    resp = locked.post("/add", data={"name": "Lilia"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")


def test_static_files_load_without_login(locked):
    assert locked.get("/static/style.css").status_code == 200


def test_login_with_right_password_opens_the_dashboard(locked):
    assert _login(locked).headers["Location"].endswith("/")
    assert locked.get("/").status_code == 200


def test_login_with_wrong_username_stays_out(locked):
    assert _login(locked, "hunter2", "someone").headers["Location"].endswith("/login")
    assert locked.get("/").status_code == 302


def test_login_with_wrong_password_stays_out(locked):
    assert _login(locked, "nope").headers["Location"].endswith("/login")
    assert locked.get("/").status_code == 302


def test_login_discards_the_pre_login_session(locked):
    locked.get("/login")
    with locked.session_transaction() as sess:
        before = sess["csrf_token"]
    _login(locked)
    with locked.session_transaction() as sess:
        assert sess.get("csrf_token") != before


def test_login_requires_a_csrf_token(locked):
    assert locked.post("/login", data={"password": "hunter2"}).status_code == 400


def test_logout_signs_out(locked):
    _login(locked)
    locked.get("/")  # renders the sign-out form, minting a token
    with locked.session_transaction() as sess:
        token = sess["csrf_token"]
    assert locked.post("/logout", data={"csrf_token": token}).status_code == 302
    assert locked.get("/").status_code == 302


def test_a_session_for_a_deleted_account_is_signed_out(locked):
    _login(locked)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM users")
    assert locked.get("/").status_code == 302


def test_repeated_failures_are_throttled(locked):
    for _ in range(auth.LOGIN_FAILURES):
        _login(locked, "nope")
    assert _login(locked, "hunter2").status_code == 429
    assert locked.get("/").status_code == 302


def test_malformed_hash_fails_closed():
    db.init_db()
    db.create_user("admin", "garbage")
    assert auth.authenticate("admin", "garbage") is None


def test_spending_buttons_are_rate_limited_per_ip(monkeypatch, locked):
    monkeypatch.setenv("DASHBOARD_ACTIONS_PER_MINUTE", "2")
    client = _new_client()
    _login(client)
    with client.session_transaction() as sess:
        sess["csrf_token"] = "t"
    codes = [client.post("/add", data={"csrf_token": "t", "name": ""}).status_code
             for _ in range(3)]
    assert codes == [302, 302, 429]


def test_rate_limit_leaves_other_posts_alone(monkeypatch, locked):
    monkeypatch.setenv("DASHBOARD_ACTIONS_PER_MINUTE", "1")
    client = _new_client()
    _login(client)
    with client.session_transaction() as sess:
        sess["csrf_token"] = "t"
    for _ in range(3):
        resp = client.post("/verify-selected", data={"csrf_token": "t"})
        assert resp.status_code == 302


def test_rate_limiter_window_slides():
    now = [0.0]
    limiter = auth.RateLimiter(2, 60, clock=lambda: now[0])
    assert limiter.hit("a") and limiter.hit("a") and not limiter.hit("a")
    assert limiter.hit("b")
    now[0] = 61
    assert limiter.hit("a")


def test_rate_limiter_zero_means_off():
    limiter = auth.RateLimiter(0, 60)
    assert all(limiter.hit("a") for _ in range(100))


@pytest.mark.parametrize("host,ok", [("127.0.0.1", True), ("localhost", True), ("::1", True),
                                     ("0.0.0.0", False), ("192.168.1.5", False)])
def test_binding_beyond_loopback_needs_closed_or_coded_signup(host, ok, monkeypatch):
    if ok:
        auth.require_safe_binding(host)
    else:
        with pytest.raises(SystemExit):
            auth.require_safe_binding(host)
    monkeypatch.setenv("DASHBOARD_SIGNUP_CODE", "x")
    auth.require_safe_binding(host)


def test_closed_signup_beyond_loopback_needs_an_account(monkeypatch):
    db.init_db()
    monkeypatch.setenv("DASHBOARD_SIGNUP", "off")
    with pytest.raises(SystemExit):
        auth.require_safe_binding("0.0.0.0")
    _make_user()
    auth.require_safe_binding("0.0.0.0")


# --- sign-up ---------------------------------------------------------------


def _signup(client, username="newuser", password="longenough", confirm=None, code=None):
    client.get("/signup")
    with client.session_transaction() as sess:
        token = sess["csrf_token"]
    data = {"username": username, "password": password,
            "confirm": password if confirm is None else confirm, "csrf_token": token}
    if code is not None:
        data["code"] = code
    return client.post("/signup", data=data)


@pytest.fixture
def anon():
    return _new_client()


def test_signup_creates_an_account_and_signs_in(anon):
    resp = _signup(anon)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/")
    assert anon.get("/").status_code == 200
    assert db.count_users() == 1


def test_the_new_account_can_sign_in_from_another_device(anon):
    _signup(anon)
    other = _new_client()
    assert _login(other, "longenough", "NewUser").headers["Location"].endswith("/")
    assert other.get("/").status_code == 200
    assert _login(_new_client(), "wrong", "newuser").headers["Location"].endswith("/login")


def test_password_is_stored_hashed(anon):
    _signup(anon)
    assert db.get_user_by_name("newuser")["password_hash"] != "longenough"


@pytest.mark.parametrize("kwargs", [
    {"username": "ab"}, {"username": "has space"}, {"username": "x" * 33},
    {"password": "short"}, {"password": "longenough", "confirm": "different1"}])
def test_signup_rejects_bad_input(anon, kwargs):
    resp = _signup(anon, **kwargs)
    assert resp.headers["Location"].endswith("/signup")
    assert db.count_users() == 0


def test_usernames_are_unique_ignoring_case(anon):
    _signup(anon, "Taken")
    assert _signup(_new_client(), "taken").headers["Location"].endswith("/signup")
    assert db.count_users() == 1


def test_signup_code_is_required_when_set(monkeypatch, anon):
    monkeypatch.setenv("DASHBOARD_SIGNUP_CODE", "sesame")
    assert b"Sign-up code" in anon.get("/signup").data
    assert _signup(anon, code="wrong").headers["Location"].endswith("/signup")
    assert _signup(anon).headers["Location"].endswith("/signup")
    assert db.count_users() == 0
    assert _signup(anon, code="sesame").headers["Location"].endswith("/")
    assert db.count_users() == 1


def test_signup_off_hides_and_blocks_it(monkeypatch, anon):
    monkeypatch.setenv("DASHBOARD_SIGNUP", "off")
    assert anon.get("/signup").status_code == 302
    assert b"Create an account" not in anon.get("/login").data
    anon.get("/login")
    with anon.session_transaction() as sess:
        token = sess["csrf_token"]
    anon.post("/signup", data={"username": "newuser", "password": "longenough",
                               "confirm": "longenough", "csrf_token": token})
    assert db.count_users() == 0


def test_signup_attempts_are_throttled(monkeypatch, anon):
    monkeypatch.setenv("DASHBOARD_SIGNUP_CODE", "sesame")
    for _ in range(auth.SIGNUP_ATTEMPTS):
        _signup(anon, code="guess")
    assert _signup(anon, code="sesame").status_code == 429
    assert db.count_users() == 0


def test_signup_needs_a_csrf_token(anon):
    assert anon.post("/signup", data={"username": "newuser", "password": "longenough",
                                      "confirm": "longenough"}).status_code == 400
