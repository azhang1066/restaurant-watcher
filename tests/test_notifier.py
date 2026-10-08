"""Tests for notifier's config handling. These exist because the config used
to be read into module-level constants at import time, which made import
order decide whether `.env` was seen -- importing notifier before
load_dotenv() ran silently gave you the default topic and no email. Reading
at call time is the fix; these tests pin it down by setting the environment
*after* import, exactly as that bug required."""
import smtplib
from email.header import decode_header, make_header

import notifier


class _FakeSMTP:
    """Records what a send would have done, without touching the network."""
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.started_tls = False
        self.login_args = None
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True
        self.tls_context = context

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


def _patch_smtp(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


def _patch_ntfy(monkeypatch):
    posts = []
    monkeypatch.setattr(
        notifier._session, "post",
        lambda url, **kwargs: posts.append({"url": url, **kwargs}),
    )
    return posts


def _title(post):
    """The Title header read back the way ntfy reads it: RFC 2047 in, the
    original text out. Asserting on the raw header instead would pin the
    encoding rather than what lands on the phone."""
    return str(make_header(decode_header(post["headers"]["Title"])))


def _clear_email_env(monkeypatch):
    for var in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
                "EMAIL_FROM", "EMAIL_TO"):
        monkeypatch.delenv(var, raising=False)


def test_ntfy_topic_read_at_call_time(monkeypatch):
    _clear_email_env(monkeypatch)
    posts = _patch_ntfy(monkeypatch)
    monkeypatch.setenv("NTFY_TOPIC", "set-after-import")

    notifier.notify("Title", "Body")

    assert posts[0]["url"] == "https://ntfy.sh/set-after-import"


def test_ntfy_is_not_sent_without_a_private_topic(monkeypatch):
    """The default topic is a public, guessable feed. Nothing may be posted to
    it -- including the placeholder from .env.example."""
    _clear_email_env(monkeypatch)
    posts = _patch_ntfy(monkeypatch)

    for topic in (None, notifier.DEFAULT_NTFY_TOPIC, "pick-a-unique-topic-name"):
        if topic is None:
            monkeypatch.delenv("NTFY_TOPIC", raising=False)
        else:
            monkeypatch.setenv("NTFY_TOPIC", topic)
        notifier.notify("Title", "Body")

    assert posts == []


def test_email_still_sent_when_ntfy_topic_is_unset(monkeypatch):
    _clear_email_env(monkeypatch)
    _patch_ntfy(monkeypatch)
    fake = _patch_smtp(monkeypatch)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EMAIL_FROM", "a@example.com")
    monkeypatch.setenv("EMAIL_TO", "b@example.com")

    notifier.notify("Title", "Body")

    assert len(fake.instances[0].sent) == 1


def test_starttls_verifies_the_server_certificate(monkeypatch):
    import ssl
    _clear_email_env(monkeypatch)
    monkeypatch.setenv("NTFY_TOPIC", "private-topic")
    _patch_ntfy(monkeypatch)
    fake = _patch_smtp(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EMAIL_FROM", "a@example.com")
    monkeypatch.setenv("EMAIL_TO", "b@example.com")

    notifier.notify("Title", "Body")

    context = fake.instances[0].tls_context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_port_465_uses_implicit_ssl(monkeypatch):
    _clear_email_env(monkeypatch)
    monkeypatch.setenv("NTFY_TOPIC", "private-topic")
    _patch_ntfy(monkeypatch)
    used = {}

    class _FakeSMTPSSL(_FakeSMTP):
        def __init__(self, host, port, timeout=None, context=None):
            super().__init__(host, port, timeout)
            used["context"] = context

    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPSSL)
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: 1 / 0)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("EMAIL_FROM", "a@example.com")
    monkeypatch.setenv("EMAIL_TO", "b@example.com")

    notifier.notify("Title", "Body")

    assert used["context"] is not None
    assert not _FakeSMTP.instances[-1].started_tls
    assert len(_FakeSMTP.instances[-1].sent) == 1


def test_email_skipped_when_unconfigured(monkeypatch):
    _clear_email_env(monkeypatch)
    _patch_ntfy(monkeypatch)
    fake = _patch_smtp(monkeypatch)

    notifier.notify("Title", "Body")

    assert fake.instances == []


def test_email_sent_when_configured_after_import(monkeypatch):
    _clear_email_env(monkeypatch)
    _patch_ntfy(monkeypatch)
    fake = _patch_smtp(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "user@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "hunter2")
    monkeypatch.setenv("EMAIL_FROM", "from@example.com")
    monkeypatch.setenv("EMAIL_TO", "to@example.com")

    notifier.notify("Closed", "Body text", url="https://maps/x")

    server = fake.instances[0]
    assert (server.host, server.port) == ("smtp.example.com", 587)
    assert server.started_tls
    assert server.login_args == ("user@example.com", "hunter2")
    sent = server.sent[0]
    assert sent["Subject"] == "Closed"
    assert sent["From"] == "from@example.com"
    assert sent["To"] == "to@example.com"
    assert "https://maps/x" in sent.get_content()


def test_email_from_defaults_to_smtp_user(monkeypatch):
    _clear_email_env(monkeypatch)
    _patch_ntfy(monkeypatch)
    fake = _patch_smtp(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "user@example.com")
    monkeypatch.setenv("EMAIL_TO", "to@example.com")

    notifier.notify("Title", "Body")

    assert fake.instances[0].sent[0]["From"] == "user@example.com"


def test_email_failure_does_not_propagate(monkeypatch):
    _clear_email_env(monkeypatch)
    posts = _patch_ntfy(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EMAIL_FROM", "from@example.com")
    monkeypatch.setenv("EMAIL_TO", "to@example.com")

    def _boom(*args, **kwargs):
        raise OSError("mail server down")

    monkeypatch.setattr(smtplib, "SMTP", _boom)

    notifier.notify("Title", "Body")  # must not raise

    assert len(posts) == 1  # the ntfy alert still went out


def test_non_ascii_title_is_encoded_for_the_header(monkeypatch):
    """Every title below carries an emoji, and ntfy takes the title as an
    HTTP header -- which http.client encodes as latin-1. Unencoded, that
    raised UnicodeEncodeError before the request left, so the alert was
    lost and run_check logged it as a failed check. RFC 2047 is what ntfy
    decodes back to the original text."""
    _clear_email_env(monkeypatch)
    posts = _patch_ntfy(monkeypatch)

    notifier.notify("\U0001F6AB Lilia is permanently closed", "Body")

    raw = posts[0]["headers"]["Title"]
    raw.encode("latin-1")  # must not raise -- this is the bug itself
    assert raw != "\U0001F6AB Lilia is permanently closed"   # it was encoded
    # ...and it survives the round trip, so the phone still shows the emoji.
    assert _title(posts[0]) == "\U0001F6AB Lilia is permanently closed"


def test_ascii_title_is_left_alone(monkeypatch):
    _clear_email_env(monkeypatch)
    posts = _patch_ntfy(monkeypatch)

    notifier.notify("Lilia is permanently closed", "Body")

    assert posts[0]["headers"]["Title"] == "Lilia is permanently closed"


def test_email_subject_keeps_the_original_title(monkeypatch):
    """EmailMessage does its own encoding, so the subject gets the real text
    rather than the wire-safe form built for the ntfy header."""
    _clear_email_env(monkeypatch)
    _patch_ntfy(monkeypatch)
    fake = _patch_smtp(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EMAIL_FROM", "from@example.com")
    monkeypatch.setenv("EMAIL_TO", "to@example.com")

    notifier.notify("\U0001F6AB Lilia is permanently closed", "Body")

    assert fake.instances[0].sent[0]["Subject"] == "\U0001F6AB Lilia is permanently closed"


def test_needs_verification_push_is_one_message_for_the_batch(monkeypatch):
    _clear_email_env(monkeypatch)
    posts = _patch_ntfy(monkeypatch)

    notifier.notify_needs_verification(
        [{"name": "Lilia"}, {"name": "Don Angie"}, {"name": "Katz's"}])

    assert len(posts) == 1
    assert "3 restaurants to verify" in _title(posts[0])
    body = posts[0]["data"].decode("utf-8")
    assert "Lilia, Don Angie, Katz's" in body


def test_needs_verification_push_caps_the_names_it_spells_out(monkeypatch):
    """A seeded file can leave a hundred waiting; a lock screen shows two
    lines."""
    _clear_email_env(monkeypatch)
    posts = _patch_ntfy(monkeypatch)
    names = [{"name": f"Place {n}"} for n in range(8)]

    notifier.notify_needs_verification(names)

    body = posts[0]["data"].decode("utf-8")
    assert "Place 4" in body
    assert "Place 5" not in body
    assert "and 3 more" in body


def test_needs_verification_push_links_to_the_dashboard(monkeypatch):
    _clear_email_env(monkeypatch)
    posts = _patch_ntfy(monkeypatch)
    monkeypatch.setenv("DASHBOARD_URL", "http://pi.local:5000")

    notifier.notify_needs_verification([{"name": "Lilia"}])

    assert posts[0]["headers"]["Click"] == "http://pi.local:5000"
    assert "1 restaurant to verify" in _title(posts[0])  # singular


def test_closed_alert_with_a_null_address_says_no_address(monkeypatch):
    """The column is NULL for a place without an address; the old
    .get('address', '') let None through and the push body read "None"."""
    _clear_email_env(monkeypatch)
    posts = _patch_ntfy(monkeypatch)

    notifier.notify_closed({"name": "Lilia", "address": None, "maps_url": None},
                           "CLOSED_PERMANENTLY")

    assert posts[0]["data"].decode("utf-8") == "No address on file."


def test_run_problems_push_names_the_places_google_dropped(monkeypatch):
    _clear_email_env(monkeypatch)
    posts = _patch_ntfy(monkeypatch)

    notifier.notify_run_problems(2, 10, [{"name": "Lilia"}])

    assert "2 checks failed" in _title(posts[0])
    body = posts[0]["data"].decode("utf-8")
    assert "2 of 10" in body and "Lilia" in body
    assert posts[0]["headers"]["Priority"] == "default"


def test_run_problems_push_is_high_priority_when_everything_failed(monkeypatch):
    _clear_email_env(monkeypatch)
    posts = _patch_ntfy(monkeypatch)

    notifier.notify_run_problems(3, 3, [])

    assert posts[0]["headers"]["Priority"] == "high"
