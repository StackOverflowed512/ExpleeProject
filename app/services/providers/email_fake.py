"""Deterministic fake email provider.

Used by tests and as the safe default (``EMAIL_PROVIDER=fake``). It never
opens a network connection: "sent" messages are only appended to
:attr:`FakeEmailProvider.sent`. Tests can script an error to raise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.providers.email import DeliveryResult, EmailDeliveryError, OutgoingEmail

PROVIDER_NAME = "fake"


@dataclass
class FakeEmailProvider:
    """In-memory provider that records messages instead of delivering them."""

    name: str = PROVIDER_NAME
    error: EmailDeliveryError | None = None
    sent: list[OutgoingEmail] = field(default_factory=list)
    attempts: int = 0

    def send(self, message: OutgoingEmail) -> DeliveryResult:
        self.attempts += 1
        if self.error is not None:
            raise self.error
        self.sent.append(message)
        return DeliveryResult(
            provider=self.name,
            provider_reference=f"fake-{len(self.sent)}",
        )
