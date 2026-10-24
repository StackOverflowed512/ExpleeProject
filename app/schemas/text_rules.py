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
