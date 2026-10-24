"""Readable-text extraction from HTML with BeautifulSoup.

Uses the standard-library ``html.parser`` backend, which tolerates broken
markup without extra dependencies. Non-content elements (scripts, styles,
templates, SVG/MathML, embedded frames and objects, comments) are removed
before visible text is collected. Each text node becomes one line with its
whitespace collapsed to single spaces; empty nodes are dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, ProcessingInstruction

MAX_TITLE_LENGTH = 500

_NON_CONTENT_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "math",
    "iframe",
    "frame",
    "frameset",
    "object",
    "embed",
    "canvas",
    "head",
    "title",
    "link",
    "meta",
    "button",
    "select",
    "option",
    "textarea",
    "input",
)
_WHITESPACE = re.compile(r"[\s​]+")


@dataclass(frozen=True)
class ExtractedText:
    """Title and readable text extracted from one HTML document."""

    title: str | None
    text: str
    truncated: bool


def _collapse(value: str) -> str:
    """Collapse all whitespace runs (newlines included) to single spaces."""
    return _WHITESPACE.sub(" ", value).strip()


def extract_text(
    html: bytes,
    *,
    encoding: str | None,
    max_chars: int,
) -> ExtractedText:
    """Return the page title and capped, whitespace-normalized visible text.

    ``encoding`` is the charset declared by the HTTP response, if any; when
    absent BeautifulSoup detects it from the document. Malformed markup is
    parsed best-effort and never raises for ordinary HTML defects.
    """
    soup = BeautifulSoup(html, "html.parser", from_encoding=encoding)

    title: str | None = None
    if soup.title is not None and soup.title.string:
        title = _collapse(soup.title.string)[:MAX_TITLE_LENGTH] or None

    for tag in soup(_NON_CONTENT_TAGS):
        tag.decompose()
    for node in soup.find_all(
        string=lambda s: isinstance(s, (Comment, Declaration, Doctype, ProcessingInstruction))
    ):
        node.extract()

    # In HTML, whitespace inside a text node is insignificant; each text node
    # becomes one line so block structure stays readable.
    pieces = (_collapse(piece) for piece in soup.stripped_strings)
    text = "\n".join(piece for piece in pieces if piece)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars].rstrip()
    return ExtractedText(title=title, text=text, truncated=truncated)
