"""Tests for notifier's config handling. These exist because the config used
to be read into module-level constants at import time, which made import
order decide whether `.env` was seen -- importing notifier before
load_dotenv() ran silently gave you the default topic and no email. Reading
at call time is the fix; these tests pin it down by setting the environment
*after* import, exactly as that bug required."""
import smtplib

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

    def starttls(self):
        self.started_tls = True

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


def test_ntfy_falls_back_to_default_topic(monkeypatch):
    _clear_email_env(monkeypatch)
    posts = _patch_ntfy(monkeypatch)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)

    notifier.notify("Title", "Body")

    assert posts[0]["url"].endswith(notifier.DEFAULT_NTFY_TOPIC)


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
