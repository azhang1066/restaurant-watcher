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


def _signup(client, username="newuser", password="longenough", confirm=None, code=None,
            email="new@example.com"):
    client.get("/signup")
    with client.session_transaction() as sess:
        token = sess["csrf_token"]
    data = {"username": username, "password": password, "email": email,
            "confirm": password if confirm is None else confirm, "csrf_token": token}
    if code is not None:
        data["code"] = code
    return client.post("/signup", data=data)


@pytest.fixture
def anon(mailbox):
    """A signed-out client on a server that can send mail (sign-up needs that)."""
    return _new_client()


def _confirm(mailbox, client=None):
    """Open the newest confirmation link and press Confirm."""
    token = mailbox.verifications[-1][2]
    client = client or _new_client()
    page = client.get(f"/verify/{token}")
    assert page.status_code == 200
    with client.session_transaction() as sess:
        csrf = sess["csrf_token"]
    return client.post(f"/verify/{token}", data={"csrf_token": csrf})


def test_signup_creates_an_account_and_signs_in_but_it_waits_for_the_email(anon, mailbox):
    resp = _signup(anon)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/verify-email")
    assert db.count_users() == 1
    assert mailbox.verifications[-1][:2] == ("new@example.com", "newuser")
    # Signed in, but nothing works until the address is confirmed.
    for path in ("/", "/settings", "/add/confirm"):
        assert anon.get(path).headers["Location"].endswith("/verify-email")
    assert anon.get("/verify-email").status_code == 200
    assert db.get_user_by_name("newuser")["email"] is None


def test_confirming_the_link_unlocks_the_account(anon, mailbox):
    _signup(anon)
    _confirm(mailbox)
    user = db.get_user_by_name("newuser")
    assert user["email"] == "new@example.com" and user["email_verified_at"]
    assert anon.get("/").status_code == 200


def test_opening_the_link_alone_does_not_use_it_up(anon, mailbox):
    _signup(anon)
    token = mailbox.verifications[-1][2]
    for _ in range(2):  # a mail scanner opening it
        assert _new_client().get(f"/verify/{token}").status_code == 200
    assert db.get_user_by_name("newuser")["email_verified_at"] is None
    assert _confirm(mailbox).status_code == 302
    assert db.get_user_by_name("newuser")["email_verified_at"]


def test_a_confirmation_link_works_once_and_expires(anon, mailbox):
    _signup(anon)
    _confirm(mailbox)
    token = mailbox.verifications[-1][2]
    assert _new_client().get(f"/verify/{token}").status_code == 302
    _signup(_new_client(), "second", email="two@example.com")
    with db.get_conn() as conn:
        conn.execute("UPDATE email_verifications SET expires_at = datetime('now', '-1 minute')")
    assert _new_client().get(f"/verify/{mailbox.verifications[-1][2]}").status_code == 302


def test_the_new_account_can_sign_in_from_another_device(anon, mailbox):
    _signup(anon)
    other = _new_client()
    assert _login(other, "longenough", "NewUser").headers["Location"].endswith("/")
    assert other.get("/").headers["Location"].endswith("/verify-email")  # still unconfirmed
    _confirm(mailbox)
    assert other.get("/").status_code == 200
    assert _login(_new_client(), "wrong", "newuser").headers["Location"].endswith("/login")


def test_resending_to_a_corrected_address(anon, mailbox):
    _signup(anon, email="typo@exmaple.com")
    anon.post("/verify-email", data={"email": "right@example.com",
                                     "csrf_token": _token(anon, "/verify-email")})
    assert mailbox.verifications[-1][0] == "right@example.com"
    _confirm(mailbox)
    assert db.get_user_by_name("newuser")["email"] == "right@example.com"
    # the first link was replaced, not left live
    assert len(mailbox.verifications) == 2


def test_signup_needs_an_email(anon):
    assert _signup(anon, email="").headers["Location"].endswith("/signup")
    assert _signup(anon, email="not-an-email").headers["Location"].endswith("/signup")
    assert db.count_users() == 0


def test_an_email_confirmed_elsewhere_cant_be_reused(anon, mailbox):
    db.create_user("owner", auth.hash_password("longenough"), "new@example.com", verified=True)
    assert _signup(anon).headers["Location"].endswith("/signup")
    assert db.get_user_by_name("newuser") is None


def test_the_first_to_confirm_an_address_keeps_it(mailbox):
    _signup(_new_client(), "first", email="same@example.com")
    first = mailbox.verifications[-1][2]
    _signup(_new_client(), "second", email="same@example.com")
    second = mailbox.verifications[-1][2]
    for token in (first, second):
        client = _new_client()
        client.get(f"/verify/{token}")
        with client.session_transaction() as sess:
            csrf = sess["csrf_token"]
        client.post(f"/verify/{token}", data={"csrf_token": csrf})
    assert db.get_user_by_name("first")["email"] == "same@example.com"
    assert db.get_user_by_name("second")["email"] is None


def test_signup_is_refused_when_the_server_cant_send_mail(locked):
    resp = _signup(_new_client())
    assert resp.headers["Location"].endswith("/login")
    assert db.count_users() == 1


def test_an_unconfirmed_account_isnt_locked_out_when_mail_isnt_set_up(locked):
    # No SMTP: nobody could ever confirm, so the gate stays open.
    _login(locked)
    assert locked.get("/").status_code == 200


def test_an_unconfirmed_account_is_gated_once_mail_is_set_up(locked, mailbox):
    _login(locked)
    assert locked.get("/").headers["Location"].endswith("/verify-email")
    locked.post("/logout", data={"csrf_token": _token(locked, "/verify-email")})
    assert locked.get("/").headers["Location"].endswith("/login")


def test_terminal_accounts_count_as_confirmed():
    db.init_db()
    user_id, error = auth.create_account("cli", "longenough", "longenough", "cli@example.com",
                                         verified=True)
    assert error is None
    user = db.get_user(user_id)
    assert user["email"] == "cli@example.com" and user["email_verified_at"]
    assert auth.create_account("cli2", "longenough", "longenough", "")[0] is None


def test_settings_email_change_waits_for_its_link(with_email, mailbox):
    _login(with_email)

    def post(email, current="hunter2"):
        return with_email.post("/settings/email", data={
            "email": email, "current": current, "csrf_token": _token(with_email)})

    post("new@example.com", current="wrong")
    assert mailbox.verifications == []
    post("not-an-email")
    assert mailbox.verifications == []
    db.create_user("other", auth.hash_password("longenough"), "taken@example.com",
                   verified=True)
    post("Taken@Example.com")
    assert mailbox.verifications == []
    post("New@Example.com")
    assert mailbox.verifications[-1][0] == "new@example.com"
    assert db.list_users()[0]["email"] == "admin@example.com"  # unchanged until confirmed
    _confirm(mailbox)
    assert db.list_users()[0]["email"] == "new@example.com"
    assert with_email.get("/").status_code == 200


def test_reset_links_only_go_to_confirmed_addresses(locked, mailbox):
    with db.get_conn() as conn:  # an address from before confirmation existed
        conn.execute("UPDATE users SET email = 'old@example.com', email_verified_at = NULL")
    _forgot(_new_client(), "admin")
    assert mailbox == []


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
    assert _signup(anon, code="sesame").headers["Location"].endswith("/verify-email")
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


# --- change password, reset, delete account, TLS ---------------------------------


def _token(client, path="/settings"):
    client.get(path)
    with client.session_transaction() as sess:
        return sess["csrf_token"]


def _change(client, current="hunter2", new="brand-new-pw", confirm=None):
    return client.post("/settings/password", data={
        "current": current, "password": new,
        "confirm": new if confirm is None else confirm, "csrf_token": _token(client)})


def test_settings_offers_password_and_delete_forms(locked):
    _login(locked)
    page = locked.get("/settings").data
    assert b"Change password" in page and b"Delete my account" in page


def test_change_password_works_and_old_one_stops(locked):
    _login(locked)
    assert _change(locked).headers["Location"].endswith("/settings")
    assert locked.get("/").status_code == 200  # this device stays signed in
    assert _login(_new_client(), "hunter2").headers["Location"].endswith("/login")
    assert _login(_new_client(), "brand-new-pw").headers["Location"].endswith("/")


def test_change_password_signs_out_other_devices(locked):
    other = _new_client()
    _login(other)
    assert other.get("/").status_code == 200
    _login(locked)
    _change(locked)
    assert other.get("/").status_code == 302


@pytest.mark.parametrize("kwargs", [{"current": "wrong"}, {"new": "short"},
                                    {"confirm": "different-pw"}])
def test_change_password_rejects_bad_input(locked, kwargs):
    _login(locked)
    _change(locked, **kwargs)
    assert auth.authenticate("admin", "hunter2") is not None


def test_wrong_current_password_counts_toward_the_throttle(locked):
    _login(locked)
    for _ in range(auth.LOGIN_FAILURES):
        _change(locked, current="wrong")
    assert _change(locked).status_code == 429
    assert auth.authenticate("admin", "hunter2") is not None


def test_password_forms_need_a_session_and_csrf(locked):
    assert locked.post("/settings/password", data={}).status_code == 302
    _login(locked)
    assert locked.post("/settings/password", data={"current": "hunter2"}).status_code == 400
    assert locked.post("/settings/delete-account", data={}).status_code == 400


def test_reset_password_replaces_it_and_ends_sessions(locked):
    _login(locked)
    assert auth.reset_password("ADMIN", "reset-pw-1", "reset-pw-1") is None
    assert locked.get("/").status_code == 302
    assert _login(_new_client(), "reset-pw-1").headers["Location"].endswith("/")
    assert auth.reset_password("nobody", "reset-pw-1", "reset-pw-1") == auth.NO_SUCH_USER
    assert auth.reset_password("admin", "short", "short") is not None


def _delete(client, username="admin", current="hunter2"):
    return client.post("/settings/delete-account", data={
        "username": username, "current": current, "csrf_token": _token(client)})


def test_delete_account_removes_it_and_its_list(locked):
    db.add_restaurant("Lilia", "p1", user_id=db.list_users()[0]["id"])
    survivor = db.create_user("keeper", auth.hash_password("longenough"))
    db.add_restaurant("Other", "p2", user_id=survivor)
    _login(locked)
    assert _delete(locked).headers["Location"].endswith("/login")
    assert db.get_user_by_name("admin") is None
    assert [r["name"] for r in db.list_restaurants(active_only=False)] == ["Other"]
    assert locked.get("/").status_code == 302


@pytest.mark.parametrize("kwargs", [{"current": "wrong"}, {"username": "someone-else"}])
def test_delete_account_needs_username_and_password(locked, kwargs):
    _login(locked)
    assert _delete(locked, **kwargs).headers["Location"].endswith("/settings")
    assert db.get_user_by_name("admin") is not None


def test_tls_files_unset_means_proxy(monkeypatch):
    assert auth.tls_files() is None


def test_tls_files_need_both_and_must_exist(monkeypatch, tmp_path):
    cert = tmp_path / "c.pem"
    monkeypatch.setenv("DASHBOARD_TLS_CERT", str(cert))
    with pytest.raises(SystemExit):
        auth.tls_files()
    monkeypatch.setenv("DASHBOARD_TLS_KEY", str(tmp_path / "k.pem"))
    with pytest.raises(SystemExit):  # named files missing
        auth.tls_files()
    cert.write_text("x")
    (tmp_path / "k.pem").write_text("x")
    assert auth.tls_files() == (cert, tmp_path / "k.pem")


def test_tls_makes_the_cookie_secure(monkeypatch, tmp_path):
    for name in ("c.pem", "k.pem"):
        (tmp_path / name).write_text("x")
    monkeypatch.setenv("DASHBOARD_TLS_CERT", str(tmp_path / "c.pem"))
    monkeypatch.setenv("DASHBOARD_TLS_KEY", str(tmp_path / "k.pem"))
    assert dashboard.create_app().config["SESSION_COOKIE_SECURE"] is True


# --- reset by emailed link ---------------------------------------------------------


class MailLog(list):
    """Password-reset mail (the list itself) and confirmation mail (`.verifications`)."""

    def __init__(self):
        super().__init__()
        self.verifications = []


@pytest.fixture
def mailbox(monkeypatch):
    """SMTP 'configured', mail captured instead of sent, and the background
    dispatch made synchronous."""
    sent = MailLog()
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EMAIL_FROM", "watcher@example.com")
    monkeypatch.setenv("DASHBOARD_URL", "https://watch.example.com")
    monkeypatch.setattr(dashboard, "_dispatch", lambda fn, *a: fn(*a))
    monkeypatch.setattr(dashboard, "send_password_reset",
                        lambda email, username, token: sent.append((email, username, token)))
    monkeypatch.setattr(dashboard, "send_email_verification",
                        lambda email, username, token: sent.verifications.append(
                            (email, username, token)))
    return sent


def _forgot(client, identifier):
    return client.post("/forgot", data={"identifier": identifier,
                                        "csrf_token": _token(client, "/forgot")})


def _reset(client, token, password="fresh-password", confirm=None):
    return client.post(f"/reset/{token}", data={
        "password": password, "confirm": password if confirm is None else confirm,
        "csrf_token": _token(client, "/login")})


@pytest.fixture
def with_email(locked):
    db.set_email(db.list_users()[0]["id"], "admin@example.com")
    return locked


def test_forgot_is_off_without_smtp(locked):
    resp = locked.get("/forgot")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")
    assert b"Forgot your password" not in locked.get("/login").data


def test_login_offers_forgot_when_email_is_set_up(locked, mailbox):
    assert b"Forgot your password" in locked.get("/login").data


def test_reset_by_email_end_to_end(with_email, mailbox):
    anon = _new_client()
    assert _forgot(anon, "admin@example.com").headers["Location"].endswith("/login")
    (email, username, token), = mailbox
    assert (email, username) == ("admin@example.com", "admin")
    assert anon.get(f"/reset/{token}").status_code == 200
    assert _reset(anon, token).headers["Location"].endswith("/login")
    assert _login(_new_client(), "fresh-password").headers["Location"].endswith("/")
    assert _login(_new_client(), "hunter2").headers["Location"].endswith("/login")


def test_reset_link_works_once(with_email, mailbox):
    _forgot(_new_client(), "admin")
    token = mailbox[0][2]
    anon = _new_client()
    _reset(anon, token)
    assert anon.get(f"/reset/{token}").status_code == 302
    _reset(anon, token, "another-password")
    assert auth.authenticate("admin", "fresh-password") is not None


def test_a_password_typo_doesnt_burn_the_link(with_email, mailbox):
    _forgot(_new_client(), "admin")
    token = mailbox[0][2]
    anon = _new_client()
    assert _reset(anon, token, "fresh-password", "different-one").headers["Location"].endswith(
        f"/reset/{token}")
    assert _reset(anon, token).headers["Location"].endswith("/login")


def test_reset_ends_existing_sessions(with_email, mailbox):
    other = _new_client()
    _login(other)
    _forgot(_new_client(), "admin")
    _reset(_new_client(), mailbox[0][2])
    assert other.get("/").status_code == 302


def test_forgot_answers_the_same_for_unknown_accounts(with_email, mailbox):
    known = _forgot(_new_client(), "admin")
    unknown = _forgot(_new_client(), "nobody-here")
    assert known.headers["Location"] == unknown.headers["Location"]
    assert len(mailbox) == 1


def test_no_email_on_the_account_means_no_mail(locked, mailbox):
    _forgot(_new_client(), "admin")
    assert mailbox == []


def test_expired_link_is_refused(with_email, mailbox):
    _forgot(_new_client(), "admin")
    token = mailbox[0][2]
    with db.get_conn() as conn:
        conn.execute("UPDATE password_resets SET expires_at = datetime('now', '-1 minute')")
    anon = _new_client()
    assert anon.get(f"/reset/{token}").status_code == 302
    _reset(anon, token)
    assert auth.authenticate("admin", "hunter2") is not None


def test_changing_the_password_voids_outstanding_links(with_email, mailbox):
    _forgot(_new_client(), "admin")
    _login(with_email)
    _change(with_email)
    anon = _new_client()
    assert anon.get(f"/reset/{mailbox[0][2]}").status_code == 302


def test_unknown_token_is_refused(mailbox):
    assert _new_client().get("/reset/not-a-token").status_code == 302


def test_reset_requests_are_throttled(with_email, mailbox):
    client = _new_client()
    codes = [_forgot(client, "admin").status_code for _ in range(auth.RESET_REQUESTS + 1)]
    assert codes[-1] == 429 and 429 not in codes[:-1]


def test_token_is_stored_hashed(with_email, mailbox):
    _forgot(_new_client(), "admin")
    with db.get_conn() as conn:
        stored = conn.execute("SELECT token_hash FROM password_resets").fetchone()[0]
    assert stored != mailbox[0][2]


def test_confirmation_email_links_to_the_configured_dashboard_url(monkeypatch):
    import notifier
    captured: dict = {}
    monkeypatch.setenv("DASHBOARD_URL", "https://watch.example.com/")
    monkeypatch.setattr(notifier, "_send_email",
                        lambda subject, message, url=None, to=None: captured.update(
                            url=url, to=to) or True)
    assert notifier.send_email_verification("a@example.com", "andrew", "tok9")
    assert captured == {"url": "https://watch.example.com/verify/tok9", "to": "a@example.com"}


def test_reset_email_links_to_the_configured_dashboard_url(monkeypatch):
    import notifier
    captured: dict = {}
    monkeypatch.setenv("DASHBOARD_URL", "https://watch.example.com/")
    monkeypatch.setattr(notifier, "_send_email",
                        lambda subject, message, url=None, to=None: captured.update(
                            url=url, to=to) or True)
    assert notifier.send_password_reset("a@example.com", "andrew", "tok123")
    assert captured == {"url": "https://watch.example.com/reset/tok123", "to": "a@example.com"}
