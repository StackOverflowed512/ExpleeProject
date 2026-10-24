"""Unit tests for HTML readable-text extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.providers.html_text import MAX_TITLE_LENGTH, extract_text

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "websites"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_fixture_title_and_visible_text_are_extracted() -> None:
    result = extract_text(_fixture("acme_home.html"), encoding=None, max_chars=10_000)

    assert result.title == "Acme Widgets — Industrial Widgets for Teams"
    assert result.truncated is False
    lines = result.text.splitlines()
    assert "Widgets that ship" in lines
    assert "Acme builds industrial widgets for distributed teams." in lines
    assert "Acme Widgets GmbH, Berlin" in lines
    assert "Logistics" in lines


def test_non_content_elements_are_removed() -> None:
    result = extract_text(_fixture("acme_home.html"), encoding=None, max_chars=10_000)

    for leak in (
        "SCRIPT-LEAK",
        "STYLE-LEAK",
        "JSONLD-LEAK",
        "NOSCRIPT-LEAK",
        "SVG-LEAK",
        "TEMPLATE-LEAK",
        "IFRAME-LEAK",
        "INPUT-LEAK",
        "BUTTON-LEAK",
        "INLINE-SCRIPT-LEAK",
        "Synthetic fixture",  # HTML comment
        "Meta description",
    ):
        assert leak not in result.text


def test_whitespace_is_normalized() -> None:
    html = b"<p>  a \t\t b </p>\n\n\n<p>\xc2\xa0c\xc2\xa0</p>"
    result = extract_text(html, encoding="utf-8", max_chars=1000)
    assert result.text == "a b\nc"


def test_text_is_capped_and_flagged() -> None:
    html = b"<p>" + b"word " * 1000 + b"</p>"
    result = extract_text(html, encoding="utf-8", max_chars=100)
    assert result.truncated is True
    assert len(result.text) <= 100


def test_empty_document_yields_empty_text() -> None:
    result = extract_text(b"", encoding=None, max_chars=1000)
    assert result.text == ""
    assert result.title is None


def test_script_only_page_is_empty() -> None:
    html = b"<html><head><title>App</title></head><body><script>x()</script></body></html>"
    result = extract_text(html, encoding=None, max_chars=1000)
    assert result.text == ""
    assert result.title == "App"


@pytest.mark.parametrize(
    "html",
    [
        b"<html><body><p>Unclosed <b>bold <i>italic",
        b"<<<>>><p>Still text</p></div></span>",
        b"<p>Broken &amp entity &#xZZ; text</p>",
        b"\x00\x01\x02<p>binary prefix</p>",
    ],
)
def test_malformed_html_is_parsed_best_effort(html: bytes) -> None:
    result = extract_text(html, encoding=None, max_chars=1000)
    assert isinstance(result.text, str)


def test_declared_encoding_is_respected() -> None:
    html = "<p>Café Müller</p>".encode("latin-1")
    result = extract_text(html, encoding="iso-8859-1", max_chars=1000)
    assert result.text == "Café Müller"


def test_long_title_is_capped() -> None:
    html = b"<title>" + b"T" * 2000 + b"</title><p>x</p>"
    result = extract_text(html, encoding=None, max_chars=1000)
    assert result.title is not None
    assert len(result.title) == MAX_TITLE_LENGTH
