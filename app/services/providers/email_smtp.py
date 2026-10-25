"""SMTP email adapter (Python standard library only).

Security: the connection is always encrypted and verified. ``ssl`` uses
implicit TLS (``SMTP_SSL``); ``starttls`` requires the server to offer
STARTTLS and upgrades before authenticating. Both use
``ssl.create_default_context()``, so certificates and hostnames are always
verified. There is no plaintext mode and no fallback to one.

Delivery ambiguity: the MAIL, RCPT, and DATA steps are run explicitly so the
adapter knows when the message content starts to be transmitted.

* Any failure **before** the content is sent (connect, TLS, auth, MAIL,
  RCPT, DATA rejected, timeouts) is a definite failure: nothing was
  accepted.
* An explicit non-250 reply to the end of the message is a definite
  rejection.
* A timeout, disconnect, or socket error **after** the content is sent is
  :class:`EmailDeliveryUncertainError`: the server may have accepted the
  message, so it must not be retried automatically.

The adapter never retries, and never logs the password, message content,
or recipient address.
"""

from __future__ import annotations

import logging
import re
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import formatdate

from app.services.providers.email import (
    DeliveryResult,
    EmailAuthenticationError,
    EmailConnectionError,
    EmailDeliveryError,
    EmailDeliveryUncertainError,
    EmailNotConfiguredError,
    EmailRejectedError,
    EmailTimeoutError,
    EmailTlsError,
    OutgoingEmail,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "smtp"
MAX_REFERENCE_LENGTH = 200
_DOT_AT_LINE_START = re.compile(rb"(?m)^\.")
_UNPRINTABLE = re.compile(r"[^\x20-\x7e]+")


def _reference(reply: bytes | str) -> str | None:
    """Sanitized server reply text (for example a queue ID) for the audit log."""
    text = reply.decode("utf-8", "replace") if isinstance(reply, bytes) else reply
    cleaned = _UNPRINTABLE.sub(" ", text).strip()
    return cleaned[:MAX_REFERENCE_LENGTH] or None


class SmtpEmailProvider:
    """Deliver approved messages through an SMTP server over TLS."""

    name = PROVIDER_NAME
    # Overridable in tests; never replaced at runtime.
    smtp_class: type[smtplib.SMTP] = smtplib.SMTP
    smtp_ssl_class: type[smtplib.SMTP_SSL] = smtplib.SMTP_SSL

    def __init__(
        self,
        *,
        host: str | None,
        port: int,
        security: str,
        username: str | None = None,
        password: str | None = None,
        timeout_seconds: float = 20.0,
        context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
    ) -> None:
        self._host = host
        self._port = port
        self._security = security
        self._username = username
        self._password = password
        self._timeout = timeout_seconds
        self._context_factory = context_factory

    def __repr__(self) -> str:  # never expose credentials
        return (
            f"SmtpEmailProvider(host={self._host!r}, port={self._port!r}, "
            f"security={self._security!r})"
        )

    # -- public ------------------------------------------------------------
    def send(self, message: OutgoingEmail) -> DeliveryResult:
        if not self._host:
            raise EmailNotConfiguredError(
                "SMTP delivery is selected but SMTP_HOST is not configured."
            )
        if self._security not in ("starttls", "ssl"):
            raise EmailNotConfiguredError("SMTP_SECURITY must be 'starttls' or 'ssl'.")
        if bool(self._username) != bool(self._password):
            raise EmailNotConfiguredError("SMTP_USERNAME and SMTP_PASSWORD must be set together.")

        payload = self._build(message)
        context = self._context_factory()
        client = self._connect(context)
        try:
            self._prepare(client, context)
            self._envelope(client, message.sender, message.recipient)
            reference = self._transmit(client, payload)
        finally:
            self._close(client)
        logger.info("SMTP server %s accepted message %s", self._host, message.message_id)
        return DeliveryResult(provider=self.name, provider_reference=reference)

    # -- steps ---------------------------------------------------------------
    @staticmethod
    def _build(message: OutgoingEmail) -> bytes:
        try:
            mime = EmailMessage(policy=SMTP_POLICY)
            mime["From"] = message.sender
            mime["To"] = message.recipient
            mime["Subject"] = message.subject
            mime["Date"] = formatdate(usegmt=True)
            mime["Message-ID"] = message.message_id
            mime.set_content(message.body)
            return mime.as_bytes()
        except (ValueError, TypeError):
            # For example a header containing a line break.
            raise EmailRejectedError("The message could not be encoded safely.") from None

    def _connect(self, context: ssl.SSLContext) -> smtplib.SMTP:
        try:
            if self._security == "ssl":
                return self.smtp_ssl_class(
                    self._host, self._port, timeout=self._timeout, context=context
                )
            return self.smtp_class(self._host, self._port, timeout=self._timeout)
        except ssl.SSLError:
            raise EmailTlsError("A verified TLS connection to the mail server failed.") from None
        except TimeoutError:
            raise EmailTimeoutError("The mail server did not respond in time.") from None
        except (smtplib.SMTPException, OSError):
            raise EmailConnectionError("The mail server could not be reached.") from None

    def _prepare(self, client: smtplib.SMTP, context: ssl.SSLContext) -> None:
        try:
            client.ehlo()
            if self._security == "starttls":
                if not client.has_extn("starttls"):
                    raise EmailTlsError("The mail server does not offer STARTTLS.")
                client.starttls(context=context)
                client.ehlo()
            if self._username and self._password:
                client.login(self._username, self._password)
        except EmailDeliveryError:
            raise
        except smtplib.SMTPAuthenticationError:
            raise EmailAuthenticationError(
                "The mail server rejected the configured credentials."
            ) from None
        except smtplib.SMTPNotSupportedError:
            raise EmailAuthenticationError(
                "The mail server does not support the required authentication."
            ) from None
        except ssl.SSLError:
            raise EmailTlsError("A verified TLS connection to the mail server failed.") from None
        except TimeoutError:
            raise EmailTimeoutError("The mail server did not respond in time.") from None
        except (smtplib.SMTPException, OSError):
            raise EmailConnectionError("The mail server connection failed.") from None

    @staticmethod
    def _envelope(client: smtplib.SMTP, sender: str, recipient: str) -> None:
        try:
            code, _ = client.mail(sender)
            if code != 250:
                raise EmailRejectedError("The mail server refused the sender.")
            code, _ = client.rcpt(recipient)
            if code not in (250, 251):
                raise EmailRejectedError("The mail server refused the recipient.")
            code, _ = client.docmd("DATA")
            if code != 354:
                raise EmailRejectedError("The mail server refused to accept the message.")
        except EmailDeliveryError:
            raise
        except TimeoutError:
            raise EmailTimeoutError("The mail server did not respond in time.") from None
        except (smtplib.SMTPException, OSError):
            raise EmailConnectionError("The mail server connection failed.") from None

    @staticmethod
    def _transmit(client: smtplib.SMTP, payload: bytes) -> str | None:
        # From here on the server may receive the complete message, so any
        # error without an explicit reply is an uncertain outcome.
        data = _DOT_AT_LINE_START.sub(b"..", payload)
        if not data.endswith(b"\r\n"):
            data += b"\r\n"
        try:
            client.send(data + b".\r\n")
            code, reply = client.getreply()
        except (smtplib.SMTPException, OSError):
            raise EmailDeliveryUncertainError(
                "The message was transmitted but the mail server's answer was not "
                "received; it may have been delivered."
            ) from None
        if code != 250:
            raise EmailRejectedError("The mail server rejected the message.")
        return _reference(reply)

    @staticmethod
    def _close(client: smtplib.SMTP) -> None:
        try:
            client.quit()
        except (smtplib.SMTPException, OSError):
            try:
                client.close()
            except OSError:
                pass
