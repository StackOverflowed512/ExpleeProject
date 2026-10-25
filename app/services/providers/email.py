"""Email provider interface, message/result contract, and error types.

Delivery business logic depends only on :class:`EmailProvider`; adapters
(``email_smtp``, ``email_fake``) are selected in
:func:`app.services.providers.build_email_provider`.

Errors distinguish two situations that must be handled differently:

* :class:`EmailDeliveryError` with ``uncertain = False``: the message was
  definitely **not** accepted (for example a connection, TLS,
  authentication, sender, or recipient failure before the content was
  transmitted, or an explicit rejection reply). Retrying is safe.
* :class:`EmailDeliveryUncertainError` (``uncertain = True``): the content
  was transmitted but the outcome is unknown (for example the connection
  dropped while waiting for the final reply). The message **may have been
  delivered**, so the caller must not retry automatically.

Messages never include credentials, server replies with recipient data,
or message content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class EmailDeliveryError(Exception):
    """A definite delivery failure: the message was not accepted."""

    code = "email_delivery_failed"
    http_status = 502
    uncertain = False


class EmailNotConfiguredError(EmailDeliveryError):
    """The provider is missing required configuration."""

    code = "email_not_configured"
    http_status = 503


class EmailConnectionError(EmailDeliveryError):
    """The mail server could not be reached."""

    code = "email_connection_failed"


class EmailTlsError(EmailDeliveryError):
    """A secure connection could not be established or verified."""

    code = "email_tls_failed"


class EmailAuthenticationError(EmailDeliveryError):
    """The mail server rejected the configured credentials."""

    code = "email_authentication_failed"


class EmailTimeoutError(EmailDeliveryError):
    """The server did not respond in time before the content was sent."""

    code = "email_timeout"
    http_status = 504


class EmailRejectedError(EmailDeliveryError):
    """The server explicitly refused the sender, recipient, or message."""

    code = "email_rejected"


class EmailDeliveryUncertainError(EmailDeliveryError):
    """The content was transmitted but acceptance could not be confirmed."""

    code = "email_delivery_uncertain"
    uncertain = True


@dataclass(frozen=True)
class OutgoingEmail:
    """A fully approved, plain-text message ready for delivery."""

    sender: str
    recipient: str
    subject: str
    body: str
    message_id: str


@dataclass(frozen=True)
class DeliveryResult:
    """What the provider reported after accepting a message."""

    provider: str
    provider_reference: str | None = None


@runtime_checkable
class EmailProvider(Protocol):
    """Contract every email delivery adapter must satisfy."""

    name: str

    def send(self, message: OutgoingEmail) -> DeliveryResult:
        """Deliver ``message`` or raise an :class:`EmailDeliveryError`.

        Implementations must raise :class:`EmailDeliveryUncertainError`
        whenever the message may have been accepted despite the error, and
        must never retry a transmission themselves.
        """
        ...
