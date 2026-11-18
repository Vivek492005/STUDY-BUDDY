"""B-2: mailer unit tests (stdlib SMTP, no network)."""
import smtplib

import pytest

from studybuddy import mailer

MAIL_ENV_VARS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
                 "MAIL_FROM", "SMTP_USE_TLS")


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.tls_started = False
        self.logged_in = None
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self):
        self.tls_started = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in MAIL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    _FakeSMTP.instances.clear()


def _configured(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "user@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-key")
    monkeypatch.setenv("MAIL_FROM", "noreply@example.com")


def test_not_configured_without_credentials():
    assert mailer.is_mail_configured() is False
    assert mailer.send_mail("a@b.com", "s", "body") is False


def test_send_mail_uses_smtp_with_tls(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    assert mailer.send_mail("to@example.com", "Hi", "hello",
                            "<p>hello</p>") is True
    assert len(_FakeSMTP.instances) == 1
    fake = _FakeSMTP.instances[0]
    assert fake.host == "smtp-relay.brevo.com"
    assert fake.port == 587
    assert fake.tls_started is True
    assert fake.logged_in == ("user@example.com", "smtp-key")
    assert len(fake.sent) == 1
    msg = fake.sent[0]
    assert msg["To"] == "to@example.com"
    assert msg["From"] == "noreply@example.com"
    assert msg["Subject"] == "Hi"
    assert "hello" in msg.get_body(preferencelist=("plain",)).get_content()


def test_send_mail_smtp_failure_returns_false(monkeypatch):
    _configured(monkeypatch)

    class _BoomSMTP(_FakeSMTP):
        def send_message(self, msg):
            raise smtplib.SMTPException("relay down")

    monkeypatch.setattr(smtplib, "SMTP", _BoomSMTP)
    assert mailer.send_mail("to@example.com", "Hi", "hello") is False


def test_send_mail_port_465_uses_ssl(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    assert mailer.send_mail("to@example.com", "Hi", "hello") is True
    fake = _FakeSMTP.instances[0]
    assert fake.port == 465
    assert fake.tls_started is False  # implicit TLS, no STARTTLS


def test_send_mail_bad_port_falls_back_to_default(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setenv("SMTP_PORT", "banana")
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    assert mailer.send_mail("to@example.com", "Hi", "hello") is True
    assert _FakeSMTP.instances[0].port == 587


def test_reset_email_contains_link(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    link = "https://app.example.com/reset-password/abc123"
    assert mailer.send_password_reset_email(
        "to@example.com", link) is True
    msg = _FakeSMTP.instances[0].sent[0]
    assert msg["Subject"] == mailer.RESET_SUBJECT
    assert link in msg.get_body(preferencelist=("plain",)).get_content()
    assert link in msg.get_body(preferencelist=("html",)).get_content()


def test_reset_email_missing_args_returns_false(monkeypatch):
    _configured(monkeypatch)
    assert mailer.send_password_reset_email("to@example.com", "") is False
    assert mailer.send_password_reset_email(
        "", "https://x/reset") is False
