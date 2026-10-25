"""Unit tests for recipient validation, email settings, and email providers.

The SMTP adapter is exercised against a scripted fake ``smtplib`` client.
No network connection is opened and no email is sent.
"""

from __future__ import annotations

import smtplib
import socket
import ssl
from email import message_from_bytes
from email.policy import default as default_policy
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas.text_rules import normalize_email_address
from app.services.providers import (
    EmailProvider,
    FakeEmailProvider,
    SmtpEmailProvider,
    build_email_provider,
)
from app.services.providers.email import (
    EmailAuthenticationError,
    EmailConnectionError,
    EmailDeliveryUncertainError,
    EmailNotConfiguredError,
    EmailRejectedError,
    EmailTimeoutError,
    EmailTlsError,
    OutgoingEmail,
)

PASSWORD = "smtp-secret-password"
MESSAGE = OutgoingEmail(
    sender="alex@routewise.example",
    recipient="ops@acme.com",
    subject="RouteWise for acme.com — quick idea",
    body="Hello,\n.leading dot line\nRouteWise helps freight teams plan routes faster.",
    message_id="<explee.draft-1.attempt-1@routewise.example>",
)


# -- recipient validation --------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ops@acme.com", "ops@acme.com"),
        ("  Ops.Team+sales@ACME.Com  ", "Ops.Team+sales@acme.com"),
        ("o'neil@example.co.uk", "o'neil@example.co.uk"),
        ("info@bücher.de", "info@xn--bcher-kva.de"),
        ("a@acme.com.", "a@acme.com"),
    ],
)
def test_valid_addresses_are_normalized(raw: str, expected: str) -> None:
    assert normalize_email_address(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "plainaddress",
        "a@@acme.com",
        "a@b@acme.com",
        "Alex <ops@acme.com>",
        "ops@acme.com\r\nBcc: victim@evil.com",
        "ops @acme.com",
        ".ops@acme.com",
        "ops.@acme.com",
        "o..ps@acme.com",
        '"quoted"@acme.com',
        "ops@localhost",
        "ops@[127.0.0.1]",
        "ops@127.0.0.1",
        "ops@-acme.com",
        "ops@acme.c",
        "ops@acme.123",
        "x" * 65 + "@acme.com",
        "ops@" + "a" * 250 + ".com",
        "ops@acme.com,other@acme.com",
        "ops@acme\x00.com",
    ],
)
def test_invalid_addresses_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_email_address(raw)


# -- settings ------------------------------------------------------------------------
def test_email_defaults_cannot_deliver_real_email() -> None:
    settings = Settings(_env_file=None)
    assert settings.email_provider == "fake"
    assert settings.email_dry_run is True
    assert settings.email_sender is None
    assert settings.smtp_security == "starttls"
    assert settings.approval_actor == "local-operator"


def test_smtp_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("EMAIL_DRY_RUN", "false")
    monkeypatch.setenv("EMAIL_SENDER", " Alex@RouteWise.Example ")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_SECURITY", "ssl")
    monkeypatch.setenv("SMTP_USERNAME", "alex")
    monkeypatch.setenv("SMTP_PASSWORD", f" {PASSWORD} ")
    settings = Settings(_env_file=None)
    assert settings.email_sender == "Alex@routewise.example"
    assert settings.smtp_password is not None
    assert settings.smtp_password.get_secret_value() == PASSWORD
    assert PASSWORD not in repr(settings)
    assert settings.email_dry_run is False


@pytest.mark.parametrize(
    "values",
    [
        {"email_provider": "sendgrid"},
        {"email_sender": "not-an-email"},
        {"smtp_port": 0},
        {"smtp_port": 70000},
        {"smtp_security": "none"},
        {"smtp_security": "plain"},
        {"smtp_host": "smtp example.com"},
        {"smtp_timeout_seconds": 0},
        {"smtp_username": "alex"},
        {"smtp_password": PASSWORD},
        {"approval_actor": ""},
        {"approval_actor": "has spaces"},
    ],
)
def test_invalid_email_settings_are_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_builder_selects_providers() -> None:
    assert isinstance(build_email_provider(Settings(_env_file=None)), FakeEmailProvider)
    smtp = build_email_provider(
        Settings(_env_file=None, email_provider="smtp", smtp_host="smtp.example.com")
    )
    assert isinstance(smtp, SmtpEmailProvider)
    assert isinstance(smtp, EmailProvider)
    assert isinstance(FakeEmailProvider(), EmailProvider)


# -- fake SMTP client ------------------------------------------------------------------
class FakeSMTP:
    """Scripted stand-in for smtplib.SMTP / SMTP_SSL recording every call."""

    instances: list["FakeSMTP"] = []
    script: dict[str, Any] = {}

    def __init__(self, host: str, port: int, timeout: float, context: Any = None) -> None:
        self.calls: list[tuple[str, Any]] = [("connect", (host, port, timeout, context))]
        self.sent: bytes = b""
        FakeSMTP.instances.append(self)
        if "connect" in self.script:
            raise self.script["connect"]

    def _maybe(self, name: str, default: Any = None) -> Any:
        value = self.script.get(name, default)
        if isinstance(value, BaseException):
            raise value
        return value

    def ehlo(self) -> tuple[int, bytes]:
        self.calls.append(("ehlo", None))
        return self._maybe("ehlo", (250, b"ok"))

    def has_extn(self, name: str) -> bool:
        self.calls.append(("has_extn", name))
        return self._maybe("has_extn", True)

    def starttls(self, *, context: ssl.SSLContext) -> tuple[int, bytes]:
        self.calls.append(("starttls", context))
        return self._maybe("starttls", (220, b"ready"))

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        self.calls.append(("login", user))
        return self._maybe("login", (235, b"ok"))

    def mail(self, sender: str) -> tuple[int, bytes]:
        self.calls.append(("mail", sender))
        return self._maybe("mail", (250, b"ok"))

    def rcpt(self, recipient: str) -> tuple[int, bytes]:
        self.calls.append(("rcpt", recipient))
        return self._maybe("rcpt", (250, b"ok"))

    def docmd(self, cmd: str) -> tuple[int, bytes]:
        self.calls.append(("docmd", cmd))
        return self._maybe("data", (354, b"go ahead"))

    def send(self, data: bytes) -> None:
        self.calls.append(("send", len(data)))
        self.sent = data
        self._maybe("send")

    def getreply(self) -> tuple[int, bytes]:
        self.calls.append(("getreply", None))
        return self._maybe("final", (250, b"2.0.0 Ok: queued as ABC123\r\n"))

    def quit(self) -> None:
        self.calls.append(("quit", None))
        self._maybe("quit")

    def close(self) -> None:
        self.calls.append(("close", None))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.instances = []
    FakeSMTP.script = {}
    monkeypatch.setattr(SmtpEmailProvider, "smtp_class", FakeSMTP)
    monkeypatch.setattr(SmtpEmailProvider, "smtp_ssl_class", FakeSMTP)
    return FakeSMTP


def _provider(security: str = "starttls", **overrides: Any) -> SmtpEmailProvider:
    values: dict[str, Any] = {
        "host": "smtp.example.com",
        "port": 587 if security == "starttls" else 465,
        "security": security,
        "username": "alex",
        "password": PASSWORD,
        "timeout_seconds": 7.0,
    }
    values.update(overrides)
    return SmtpEmailProvider(**values)


# -- SMTP adapter --------------------------------------------------------------------
def test_starttls_flow_and_message_construction(fake_smtp: type[FakeSMTP]) -> None:
    result = _provider().send(MESSAGE)

    (client,) = fake_smtp.instances
    assert client.names() == [
        "connect",
        "ehlo",
        "has_extn",
        "starttls",
        "ehlo",
        "login",
        "mail",
        "rcpt",
        "docmd",
        "send",
        "getreply",
        "quit",
    ]
    host, port, timeout, context = client.calls[0][1]
    assert (host, port, timeout, context) == ("smtp.example.com", 587, 7.0, None)
    tls_context = dict(client.calls)["starttls"]
    assert tls_context.verify_mode == ssl.CERT_REQUIRED
    assert tls_context.check_hostname is True
    assert dict(client.calls)["mail"] == MESSAGE.sender
    assert dict(client.calls)["rcpt"] == MESSAGE.recipient
    assert dict(client.calls)["docmd"] == "DATA"

    assert result.provider == "smtp"
    assert result.provider_reference == "2.0.0 Ok: queued as ABC123"

    assert client.sent.endswith(b"\r\n.\r\n")
    assert b"\r\n..leading dot line\r\n" in client.sent  # dot-stuffing
    parsed = message_from_bytes(client.sent[:-3], policy=default_policy)
    assert parsed["From"] == MESSAGE.sender
    assert parsed["To"] == MESSAGE.recipient
    assert parsed["Subject"] == MESSAGE.subject
    assert parsed["Message-ID"] == MESSAGE.message_id
    assert parsed["Date"]
    assert parsed.get_content_type() == "text/plain"


def test_implicit_tls_uses_smtp_ssl_with_verifying_context(fake_smtp: type[FakeSMTP]) -> None:
    _provider("ssl").send(MESSAGE)
    (client,) = fake_smtp.instances
    _, port, _, context = client.calls[0][1]
    assert port == 465
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert "starttls" not in client.names()


def test_no_login_without_credentials(fake_smtp: type[FakeSMTP]) -> None:
    _provider(username=None, password=None).send(MESSAGE)
    assert "login" not in fake_smtp.instances[0].names()


@pytest.mark.parametrize(
    "overrides",
    [{"host": None}, {"host": ""}, {"security": "plain"}, {"password": None}],
)
def test_invalid_configuration_fails_before_connecting(
    fake_smtp: type[FakeSMTP], overrides: dict[str, Any]
) -> None:
    with pytest.raises(EmailNotConfiguredError) as caught:
        _provider(**overrides).send(MESSAGE)
    assert caught.value.uncertain is False
    assert fake_smtp.instances == []


def test_missing_starttls_never_falls_back_to_plaintext(fake_smtp: type[FakeSMTP]) -> None:
    fake_smtp.script = {"has_extn": False}
    with pytest.raises(EmailTlsError):
        _provider().send(MESSAGE)
    names = fake_smtp.instances[0].names()
    assert "login" not in names and "mail" not in names


@pytest.mark.parametrize(
    ("step", "error", "expected"),
    [
        ("connect", ConnectionRefusedError("refused"), EmailConnectionError),
        ("connect", socket.gaierror("no such host"), EmailConnectionError),
        ("connect", TimeoutError("slow"), EmailTimeoutError),
        ("connect", ssl.SSLCertVerificationError("bad cert"), EmailTlsError),
        ("starttls", ssl.SSLError("handshake failed"), EmailTlsError),
        ("ehlo", smtplib.SMTPServerDisconnected("gone"), EmailConnectionError),
        (
            "login",
            smtplib.SMTPAuthenticationError(535, b"bad credentials"),
            EmailAuthenticationError,
        ),
        ("login", smtplib.SMTPNotSupportedError("no AUTH"), EmailAuthenticationError),
        ("mail", (550, b"sender refused"), EmailRejectedError),
        ("rcpt", (550, b"no such user"), EmailRejectedError),
        ("data", (554, b"no"), EmailRejectedError),
        ("mail", TimeoutError("slow"), EmailTimeoutError),
        ("rcpt", smtplib.SMTPServerDisconnected("gone"), EmailConnectionError),
        ("final", (554, b"5.7.1 rejected as spam"), EmailRejectedError),
    ],
)
def test_definite_failures_are_mapped_safely(
    fake_smtp: type[FakeSMTP], step: str, error: Any, expected: type[Exception]
) -> None:
    fake_smtp.script = {step: error}
    with pytest.raises(expected) as caught:
        _provider().send(MESSAGE)
    assert caught.value.uncertain is False  # type: ignore[attr-defined]
    message = str(caught.value)
    assert PASSWORD not in message
    assert MESSAGE.recipient not in message
    assert "bad credentials" not in message and "spam" not in message
    assert len(fake_smtp.instances) <= 1  # never retried


@pytest.mark.parametrize(
    ("step", "error"),
    [
        ("send", smtplib.SMTPServerDisconnected("connection lost mid-message")),
        ("send", BrokenPipeError("pipe")),
        ("final", TimeoutError("no final reply")),
        ("final", smtplib.SMTPServerDisconnected("gone after DATA")),
        ("final", ConnectionResetError("reset")),
    ],
)
def test_failures_after_transmission_are_uncertain(
    fake_smtp: type[FakeSMTP], step: str, error: BaseException
) -> None:
    fake_smtp.script = {step: error}
    with pytest.raises(EmailDeliveryUncertainError) as caught:
        _provider().send(MESSAGE)
    assert caught.value.uncertain is True
    assert caught.value.code == "email_delivery_uncertain"
    assert len(fake_smtp.instances) == 1


def test_quit_failure_after_acceptance_is_ignored(fake_smtp: type[FakeSMTP]) -> None:
    fake_smtp.script = {"quit": smtplib.SMTPServerDisconnected("bye")}
    assert _provider().send(MESSAGE).provider_reference == "2.0.0 Ok: queued as ABC123"
    assert "close" in fake_smtp.instances[0].names()


def test_header_injection_is_refused_before_connecting(fake_smtp: type[FakeSMTP]) -> None:
    hostile = OutgoingEmail(
        sender=MESSAGE.sender,
        recipient=MESSAGE.recipient,
        subject="Hello\r\nBcc: victim@evil.example",
        body=MESSAGE.body,
        message_id=MESSAGE.message_id,
    )
    with pytest.raises(EmailRejectedError):
        _provider().send(hostile)
    assert fake_smtp.instances == []


def test_repr_never_contains_the_password() -> None:
    assert PASSWORD not in repr(_provider())


def test_fake_provider_records_without_network() -> None:
    fake = FakeEmailProvider()
    result = fake.send(MESSAGE)
    assert fake.sent == [MESSAGE]
    assert fake.attempts == 1
    assert result.provider == "fake" and result.provider_reference == "fake-1"
    failing = FakeEmailProvider(error=EmailDeliveryUncertainError("maybe"))
    with pytest.raises(EmailDeliveryUncertainError):
        failing.send(MESSAGE)
    assert failing.sent == [] and failing.attempts == 1
