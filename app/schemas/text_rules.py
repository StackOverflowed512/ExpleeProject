"""Text rules shared by outreach input validation and draft output validation.

Drafts must never contain email addresses (none are ever provided to the
generator, so any address would be invented) or unfilled template slots.
Campaign text is checked with the same rules so it cannot force them in.
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Unfilled template slots: [Name], [Your Company], {{first_name}}, <NAME>.
_PLACEHOLDER = re.compile(r"\[[^\]\n]{1,40}\]|\{\{[^}\n]{1,40}\}\}|<[A-Z_ ]{2,30}>")


def contains_email(text: str) -> bool:
    """Whether ``text`` contains something that looks like an email address."""
    return _EMAIL.search(text) is not None


def contains_placeholder(text: str) -> bool:
    """Whether ``text`` contains an unfilled template slot such as ``[Name]``."""
    return _PLACEHOLDER.search(text) is not None


MAX_EMAIL_LENGTH = 254
_LOCAL_PART = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_DOMAIN_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TOP_LEVEL = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")


def normalize_email_address(value: str) -> str:
    """Validate a plain recipient address and return it normalized.

    Accepts only ``local@domain`` (no display name, comments, quoted local
    parts, IP literals, whitespace, or control characters). The domain is
    lowercased and converted to IDNA ASCII; the local part keeps its case.
    Raises :class:`ValueError` with a safe message otherwise.
    """
    if not isinstance(value, str):
        raise ValueError("must be a string")
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_EMAIL_LENGTH:
        raise ValueError(f"must be 1-{MAX_EMAIL_LENGTH} characters")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        raise ValueError("must not contain whitespace or control characters")
    if candidate.count("@") != 1:
        raise ValueError("must contain exactly one '@'")
    local, domain = candidate.split("@")
    if not 1 <= len(local) <= 64 or not _LOCAL_PART.match(local):
        raise ValueError("has an invalid local part")
    try:
        ascii_domain = domain.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("has an invalid domain") from None
    labels = ascii_domain.split(".")
    if (
        len(labels) < 2
        or not all(_DOMAIN_LABEL.match(label) for label in labels)
        or not _TOP_LEVEL.match(labels[-1])
    ):
        raise ValueError("has an invalid domain")
    address = f"{local}@{ascii_domain}"
    if len(address) > MAX_EMAIL_LENGTH:
        raise ValueError(f"must be 1-{MAX_EMAIL_LENGTH} characters")
    return address
