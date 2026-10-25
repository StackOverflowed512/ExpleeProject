"""Web interface security: CSRF, origin checks, forms, headers, secrets, errors."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.routing import APIRoute
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.company_candidate import CompanyCandidate
from app.models.draft_approval import DraftApproval
from app.routers.ui import campaigns, deliveries, drafts, explorer, prospects, system
from app.schemas.search import SearchResultItem
from app.services.providers.fake import FakeSearchProvider
from app.web import csrf
from app.web.forms import MAX_FORM_BYTES
from tests.integration.test_ui_outreach import draft_id  # noqa: F401 - fixture

UI_MODULES = (explorer, prospects, campaigns, drafts, deliveries, system)


def _post_paths() -> list[str]:
    paths = []
    for module in UI_MODULES:
        for route in module.router.routes:
            assert isinstance(route, APIRoute)
            assert route.methods <= {"GET", "POST", "HEAD"}, route.path
            if "POST" in route.methods:
                paths.append("/ui" + re.sub(r"\{[a-z_]+\}", "1", route.path))
    return paths


def _count(factory: sessionmaker[Session], model: Any) -> int:
    with factory() as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


SEARCH = FakeSearchProvider(
    results=[SearchResultItem(title="Acme", url="https://acme.example/", provider="fake")]
)


# -- CSRF ------------------------------------------------------------------------------
def test_every_ui_post_route_is_known() -> None:
    assert sorted(_post_paths()) == sorted(
        [
            "/ui/explorer/search",
            "/ui/companies/1/research",
            "/ui/companies/1/qualify",
            "/ui/companies/1/save",
            "/ui/campaigns",
            "/ui/campaigns/1/drafts",
            "/ui/drafts/1/approve",
            "/ui/drafts/1/send",
            "/ui/drafts/1/resolve",
        ]
    )


async def test_every_ui_post_requires_a_csrf_token(ui_factory: Any) -> None:
    async with ui_factory() as ui:
        await ui.get("/ui/explorer")  # has the cookie, but sends no token
        for path in _post_paths():
            response = await ui.post(path, {}, csrf=False)
            assert response.status_code == 403, path
            assert "security token" in response.text, path


async def test_csrf_rejects_wrong_missing_or_foreign_tokens(
    ui_factory: Any, db_session_factory: sessionmaker[Session]
) -> None:
    form = {"query": "freight", "limit": "1"}
    async with ui_factory(search=SEARCH) as other:
        await other.get("/ui/explorer")
        foreign_token = other.token
    async with ui_factory(search=SEARCH) as ui:
        no_cookie = await ui.http.post("/ui/explorer/search", data=form | {"csrf_token": "x"})
        await ui.get("/ui/explorer")
        wrong = await ui.post("/ui/explorer/search", form | {"csrf_token": "forged"})
        foreign = await ui.post("/ui/explorer/search", form | {"csrf_token": foreign_token})
        ui.http.cookies.set(csrf.COOKIE_NAME, "A" * 43, path="/ui")
        swapped_cookie = await ui.post("/ui/explorer/search", form)

    for response in (no_cookie, wrong, foreign, swapped_cookie):
        assert response.status_code == 403
        assert "Nothing was changed." in response.text
    assert _count(db_session_factory, CompanyCandidate) == 0


async def test_csrf_accepts_the_htmx_header(
    ui_factory: Any, db_session_factory: sessionmaker[Session]
) -> None:
    async with ui_factory(search=SEARCH) as ui:
        await ui.get("/ui/explorer")
        response = await ui.post(
            "/ui/explorer/search",
            {"query": "freight", "limit": "1"},
            csrf=False,
            headers={csrf.HEADER_NAME: ui.token or "", "HX-Request": "true"},
        )
    assert response.status_code == 200
    assert _count(db_session_factory, CompanyCandidate) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://test:8080"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
async def test_cross_site_requests_are_refused_even_with_a_token(
    ui_factory: Any, db_session_factory: sessionmaker[Session], headers: dict[str, str]
) -> None:
    async with ui_factory(search=SEARCH) as ui:
        await ui.get("/ui/explorer")
        response = await ui.post(
            "/ui/explorer/search", {"query": "freight", "limit": "1"}, headers=headers
        )
    assert response.status_code == 403
    assert "came from another site" in response.text
    assert _count(db_session_factory, CompanyCandidate) == 0


async def test_same_origin_requests_are_accepted(ui_factory: Any) -> None:
    async with ui_factory(search=SEARCH) as ui:
        await ui.get("/ui/explorer")
        response = await ui.post(
            "/ui/explorer/search",
            {"query": "freight", "limit": "1"},
            headers={"Origin": "http://test", "Sec-Fetch-Site": "same-origin"},
        )
    assert response.status_code == 200


async def test_csrf_cookie_attributes(ui_factory: Any) -> None:
    async with ui_factory() as ui:
        first = await ui.get("/ui/explorer")
        second = await ui.get("/ui/explorer")
    cookie = first.headers["set-cookie"]
    assert cookie.startswith(f"{csrf.COOKIE_NAME}=")
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/ui" in lowered
    assert "set-cookie" not in second.headers  # issued once, then reused
    expected = csrf.token_for(ui.app.state.csrf_secret, first.cookies[csrf.COOKIE_NAME])
    assert f'content="{expected}"' in first.text


# -- form handling -------------------------------------------------------------------------
async def test_forms_must_be_urlencoded_and_bounded(ui_factory: Any) -> None:
    async with ui_factory() as ui:
        await ui.get("/ui/explorer")
        as_json = await ui.http.post(
            "/ui/explorer/search",
            json={"query": "freight", "csrf_token": ui.token},
        )
        huge = await ui.post("/ui/explorer/search", {"query": "x" * (MAX_FORM_BYTES + 10)})
        unexpected = await ui.post("/ui/explorer/search", {"query": "freight", "admin": "1"})

    assert as_json.status_code == 415
    assert huge.status_code == 413
    assert unexpected.status_code == 422 and "unexpected fields: admin" in unexpected.text


async def test_json_api_ignores_cross_site_text_plain_posts(
    ui_factory: Any, draft_id: int, db_session_factory: sessionmaker[Session]  # noqa: F811
) -> None:
    """A cross-site HTML form can only send text/plain or form data, never JSON."""
    async with ui_factory() as ui:
        response = await ui.http.post(
            f"/drafts/{draft_id}/approve",
            content=b'{"recipient_email": "ops@acme.com", "revision": 1}',
            headers={"Content-Type": "text/plain"},
        )
    assert response.status_code == 422
    assert _count(db_session_factory, DraftApproval) == 0


# -- headers, rendering, secrets -------------------------------------------------------------
async def test_ui_pages_send_security_headers(ui_factory: Any) -> None:
    async with ui_factory() as ui:
        page = await ui.get("/ui/explorer")
        redirect = await ui.get("/ui/")
        api = await ui.http.get("/health")

    csp = page.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["cache-control"] == "no-store"
    assert "content-security-policy" in redirect.headers
    # The JSON API keeps its behaviour; it only gains nosniff-style headers.
    assert "content-security-policy" not in api.headers
    assert api.headers["x-content-type-options"] == "nosniff"


async def test_pages_have_no_inline_scripts_or_styles(ui_factory: Any) -> None:
    async with ui_factory() as ui:
        html = (await ui.get("/ui/explorer")).text
    assert re.search(r"<script(?![^>]*\bsrc=)", html) is None
    assert "<style" not in html and " style=" not in html
    assert " onclick=" not in html and "hx-on" not in html
    assert '"allowEval":false' in html


async def test_secrets_are_never_rendered(ui_factory: Any) -> None:
    secrets = {
        "tavily_api_key": SecretStr("tvly-SECRET-123"),
        "mistral_api_key": SecretStr("mistral-SECRET-456"),
        "smtp_password": SecretStr("smtp-SECRET-789"),
        "smtp_username": "mailer",
        "smtp_host": "smtp.example.com",
        "email_provider": "smtp",
    }
    async with ui_factory(**secrets) as ui:
        pages = [await ui.get(path) for path in ("/ui/system", "/ui/explorer", "/ui/drafts")]
    for page in pages:
        assert page.status_code == 200
        for secret in ("tvly-SECRET-123", "mistral-SECRET-456", "smtp-SECRET-789"):
            assert secret not in page.text
        assert "explee:explee" not in page.text  # database credentials
    system_page = pages[0].text
    assert "Configured" in system_page and "smtp.example.com" in system_page


async def test_unexpected_errors_render_a_generic_page(ui_factory: Any) -> None:
    class Broken:
        name = "broken"

        def search(self, query: str, limit: int) -> list[SearchResultItem]:
            raise RuntimeError("internal detail: /secret/path")

    async with ui_factory(search=Broken()) as ui:
        await ui.get("/ui/explorer")
        response = await ui.post("/ui/explorer/search", {"query": "freight", "limit": "1"})

    assert response.status_code == 500
    assert "An unexpected error occurred." in response.text
    assert "/secret/path" not in response.text and "Traceback" not in response.text
    assert "content-security-policy" in response.headers


async def test_notice_codes_cannot_inject_text(ui_factory: Any) -> None:
    async with ui_factory() as ui:
        hostile = await ui.get("/ui/explorer", params={"notice": "<script>alert(1)</script>"})
        known = await ui.get("/ui/explorer", params={"notice": "approved"})
    assert "alert(1)" not in hostile.text
    assert "Approval recorded for this exact draft and recipient." in known.text


async def test_redirects_stay_inside_the_ui(
    ui_factory: Any, draft_id: int  # noqa: F811
) -> None:
    async with ui_factory(email_sender="alex@routewise.example") as ui:
        approved = await ui.post(
            f"/ui/drafts/{draft_id}/approve",
            {"recipient_email": "ops@acme.com", "revision": "1", "confirm_reviewed": "yes"},
        )
        sent = await ui.post(f"/ui/drafts/{draft_id}/send", {"mode": "dry_run"})
    for response in (approved, sent):
        assert response.status_code == 303
        assert response.headers["location"].startswith(f"/ui/drafts/{draft_id}?notice=")
