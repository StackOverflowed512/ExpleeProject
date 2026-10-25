"""Web interface: navigation, Explorer, research, qualification, saved prospects.

Runs the full app against the disposable test database with fake providers.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.company_candidate import CompanyCandidate
from app.models.company_qualification import CompanyQualification
from app.models.company_research import CompanyResearch
from app.models.saved_prospect import SavedProspect
from app.schemas.search import SearchResultItem
from app.services.providers.errors import ProviderConfigurationError
from app.services.providers.fake import FakeSearchProvider
from app.services.providers.website import FetchFailure, FetchOutcome, FetchResult
from app.services.providers.website_fake import FAKE_FETCHED_AT, FakeWebsiteFetcher
from tests.integration.outreach_helpers import make_candidate, make_saved_prospect

PAGES = (
    "/ui/explorer",
    "/ui/prospects",
    "/ui/campaigns",
    "/ui/campaigns/new",
    "/ui/drafts",
    "/ui/deliveries",
    "/ui/system",
)


def _results(*names: str) -> FakeSearchProvider:
    return FakeSearchProvider(
        results=[
            SearchResultItem(
                title=name,
                url=f"https://{name.lower().replace(' ', '')}.example/",
                snippet=f"{name} builds B2B SaaS for freight teams.",
                provider="fake",
                provider_result_id=f"r-{index}",
            )
            for index, name in enumerate(names)
        ]
    )


def _count(factory: sessionmaker[Session], model: Any) -> int:
    with factory() as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


# -- shell and navigation ------------------------------------------------------------
async def test_every_page_renders_the_shell_with_empty_states(ui_factory: Any) -> None:
    async with ui_factory() as ui:
        for path in PAGES:
            response = await ui.get(path)
            assert response.status_code == 200, (path, response.text[:500])
            assert response.headers["content-type"].startswith("text/html")
            html = response.text
            assert '<main id="main"' in html
            for label in ("Explorer", "Saved prospects", "Campaigns", "Draft review",
                          "Delivery history", "System status"):
                assert label in html
            assert "Email: dry-run only, nothing is sent" in html
            assert "Do not expose it to an untrusted network" in html

        empty = {
            "/ui/explorer": "No companies discovered yet",
            "/ui/prospects": "No saved prospects yet",
            "/ui/campaigns": "No campaigns yet",
            "/ui/campaigns/new": "No saved prospects to choose from",
            "/ui/drafts": "No drafts with this status",
            "/ui/deliveries": "No delivery attempts",
        }
        for path, text in empty.items():
            assert text in (await ui.get(path)).text, path


async def test_ui_root_redirects_and_marks_the_current_page(ui_factory: Any) -> None:
    async with ui_factory() as ui:
        root = await ui.get("/ui/")
        bare = await ui.get("/ui")
        page = await ui.get("/ui/campaigns")
        api_root = await ui.http.get("/")

    assert root.status_code == bare.status_code == 303
    assert root.headers["location"] == "/ui/explorer"
    assert 'href="/ui/campaigns" class="nav-link nav-link-active" aria-current="page"' in page.text
    assert api_root.json()["ui"].endswith("/ui/")


async def test_static_assets_are_served_with_content_hashes(ui_factory: Any) -> None:
    async with ui_factory() as ui:
        page = await ui.get("/ui/explorer")
        css_url = re.search(r'href="(/static/css/app\.css\?v=[0-9a-f]{12})"', page.text)
        js_url = re.search(r'src="(/static/vendor/htmx\.min\.js\?v=[0-9a-f]{12})"', page.text)
        assert css_url and js_url
        css = await ui.http.get(css_url.group(1))
        js = await ui.http.get(js_url.group(1))
    assert css.status_code == 200 and "tailwindcss" in css.text[:200]
    assert js.status_code == 200 and 'version:"2.0.10"' in js.text


async def test_unknown_ui_pages_and_bad_ids_render_html_errors(ui_factory: Any) -> None:
    async with ui_factory() as ui:
        missing = await ui.get("/ui/nowhere")
        bad_id = await ui.get("/ui/companies/abc")
        unknown = await ui.get("/ui/companies/999")
        api_missing = await ui.http.get("/companies/999/research")

    assert missing.status_code == 404 and "Page not found" in missing.text
    assert bad_id.status_code == 422 and "<html" in bad_id.text
    assert unknown.status_code == 404 and "Company candidate 999 was not found." in unknown.text
    # The JSON API keeps its error envelope.
    assert api_missing.json()["error"]["code"] == "candidate_not_found"


# -- Explorer search ---------------------------------------------------------------------
async def test_search_persists_and_lists_results(
    ui_factory: Any, db_session_factory: sessionmaker[Session]
) -> None:
    async with ui_factory(search=_results("Acme Freight", "Beta Routes")) as ui:
        response = await ui.post("/ui/explorer/search", {"query": "freight saas", "limit": "5"})
        listing = await ui.get("/ui/explorer")

    assert response.status_code == 200
    assert "Results for “freight saas”" in response.text
    assert "2 returned · 2 new" in response.text
    assert "Snippet from the search provider, not verified." in response.text
    assert _count(db_session_factory, CompanyCandidate) == 2
    assert "Acme Freight" in listing.text and "Beta Routes" in listing.text


async def test_search_returns_a_fragment_for_htmx(ui_factory: Any) -> None:
    async with ui_factory(search=_results("Acme Freight")) as ui:
        await ui.get("/ui/explorer")
        fragment = await ui.post(
            "/ui/explorer/search",
            {"query": "freight", "limit": "3"},
            headers={"HX-Request": "true", "HX-Target": "search-results"},
        )
        boosted = await ui.post(
            "/ui/explorer/search",
            {"query": "freight", "limit": "3"},
            headers={"HX-Request": "true", "HX-Target": "search-results", "HX-Boosted": "true"},
        )

    assert fragment.status_code == 200
    assert fragment.text.lstrip().startswith('<section id="search-results"')
    assert "<html" not in fragment.text
    assert "<html" in boosted.text


async def test_search_validation_and_provider_errors_are_shown(
    ui_factory: Any, db_session_factory: sessionmaker[Session]
) -> None:
    class Unconfigured:
        name = "tavily"

        def search(self, query: str, limit: int) -> list[SearchResultItem]:
            raise ProviderConfigurationError("TAVILY_API_KEY is not configured.")

    async with ui_factory(search=Unconfigured()) as ui:
        short = await ui.post("/ui/explorer/search", {"query": "x", "limit": "5"})
        too_many = await ui.post("/ui/explorer/search", {"query": "freight", "limit": "50"})
        failing = await ui.post("/ui/explorer/search", {"query": "freight", "limit": "5"})

    assert short.status_code == 422
    assert "The search was not run" in short.text
    assert 'value="x"' in short.text  # input preserved
    assert too_many.status_code == 422
    assert failing.status_code == 503
    assert "The search did not complete" in failing.text
    assert "TAVILY_API_KEY is not configured." in failing.text
    assert "Set TAVILY_API_KEY in .env" in failing.text
    assert _count(db_session_factory, CompanyCandidate) == 0


async def test_search_output_is_escaped(ui_factory: Any) -> None:
    hostile = FakeSearchProvider(
        results=[
            SearchResultItem(
                title="<script>alert(1)</script> Corp",
                url="https://evil.example/",
                snippet='<img src=x onerror="alert(2)">',
                provider="fake",
            )
        ]
    )
    async with ui_factory(search=hostile) as ui:
        response = await ui.post("/ui/explorer/search", {"query": "<b>bold</b>", "limit": "1"})

    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Corp" in response.text
    assert '<img src=x' not in response.text
    assert "<b>bold</b>" not in response.text


# -- company detail: research, qualification, save -----------------------------------------
async def test_company_page_shows_provenance_and_no_research_yet(
    ui_factory: Any, db_session: Session
) -> None:
    candidate = make_candidate(db_session, research_text=None)
    async with ui_factory() as ui:
        page = await ui.get(f"/ui/companies/{candidate.id}")

    assert page.status_code == 200
    html = page.text
    assert "Acme Logistics" in html
    assert "Not researched" in html
    assert "No research attempts yet." in html
    assert "Third-party search text, not verified." in html
    assert "Qualify the company first." in html
    assert 'rel="noopener noreferrer nofollow"' in html


async def test_research_runs_only_on_explicit_post_and_reports_outcome(
    ui_factory: Any, db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    candidate = make_candidate(db_session, research_text=None)
    url = candidate.website_url
    fetcher = FakeWebsiteFetcher(
        results={
            url: FetchResult(
                requested_url=url,
                fetched_at=datetime.now(UTC),  # fresh, so a second request reuses it
                outcome=FetchOutcome.SUCCESS,
                fetcher="fake",
                final_url=url,
                http_status=200,
                content_type="text/html",
                page_title="Acme | Freight software",
                text="Acme is a B2B SaaS platform for freight dispatchers.",
            )
        }
    )
    async with ui_factory(fetcher=fetcher) as ui:
        await ui.get(f"/ui/companies/{candidate.id}")
        assert fetcher.calls == []  # viewing never fetches
        done = await ui.post(f"/ui/companies/{candidate.id}/research", {"refresh": "false"})
        page = await ui.get(done.headers["location"])
        reused = await ui.post(f"/ui/companies/{candidate.id}/research", {"refresh": "false"})

    assert done.status_code == 303
    assert done.headers["location"] == f"/ui/companies/{candidate.id}?notice=research_success"
    assert "Website research finished: usable content stored." in page.text
    assert "Researched" in page.text and "Acme | Freight software" in page.text
    assert reused.headers["location"].endswith("notice=research_reused")
    assert fetcher.calls == [url]
    assert _count(db_session_factory, CompanyResearch) == 1


async def test_failed_research_is_reported_without_inventing_content(
    ui_factory: Any, db_session: Session
) -> None:
    candidate = make_candidate(db_session, research_text=None)
    async with ui_factory(fetcher=FakeWebsiteFetcher()) as ui:
        done = await ui.post(f"/ui/companies/{candidate.id}/research", {"refresh": "true"})
        page = await ui.get(done.headers["location"])

    assert done.headers["location"].endswith("notice=research_failed")
    assert "Research failed" in page.text
    assert "The website could not be reached." in page.text
    assert "Extracted page text" not in page.text


async def test_research_without_a_website_is_refused(
    ui_factory: Any, db_session_factory: sessionmaker[Session], clean_database: None
) -> None:
    with db_session_factory() as session:
        candidate = CompanyCandidate(
            name="No Site", provider="fake", source_url="https://search.example/r"
        )
        session.add(candidate)
        session.commit()
        candidate_id = candidate.id
    async with ui_factory() as ui:
        page = await ui.get(f"/ui/companies/{candidate_id}")
        refused = await ui.post(f"/ui/companies/{candidate_id}/research", {"refresh": "false"})

    assert "cannot be researched" in page.text
    assert "Research website" not in page.text
    assert refused.status_code == 409
    assert "That action was not completed" in refused.text


async def test_qualify_form_validates_and_records_runs(
    ui_factory: Any, db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    candidate = make_candidate(db_session)
    form = {
        "profile": "freight",
        "target_keywords": "saas, b2b",
        "target_locations": "",
        "target_tlds": "",
        "excluded_keywords": "",
        "threshold": "60",
        "min_evidence_coverage": "0.5",
    }
    async with ui_factory() as ui:
        invalid = await ui.post(
            f"/ui/companies/{candidate.id}/qualify",
            form | {"target_keywords": "", "threshold": "500"},
        )
        done = await ui.post(f"/ui/companies/{candidate.id}/qualify", form)
        page = await ui.get(done.headers["location"])

    assert invalid.status_code == 422
    assert "Please correct the highlighted fields." in invalid.text
    assert 'aria-invalid="true"' in invalid.text
    assert done.status_code == 303 and done.headers["location"].endswith("notice=qualified")
    assert "Qualified" in page.text
    assert "Factors and evidence" in page.text
    assert "qualification-rules/" in page.text
    assert "Save as prospect" in page.text
    assert _count(db_session_factory, CompanyQualification) == 1


async def test_save_is_enforced_by_the_backend(
    ui_factory: Any, db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    unqualified = make_candidate(
        db_session, name="Plain Co", domain="plain.com", research_text=None
    )
    async with ui_factory() as ui:
        # No qualification yet: the backend refuses even though the form was posted.
        refused = await ui.post(f"/ui/companies/{unqualified.id}/save")
    assert refused.status_code == 409
    assert "That action was not completed" in refused.text
    assert _count(db_session_factory, SavedProspect) == 0

    qualified = make_candidate(db_session, name="Good Co", domain="good.com")
    form = {
        "profile": "freight",
        "target_keywords": "saas",
        "target_locations": "",
        "target_tlds": "",
        "excluded_keywords": "",
        "threshold": "60",
        "min_evidence_coverage": "0.5",
    }
    async with ui_factory() as ui:
        await ui.post(f"/ui/companies/{qualified.id}/qualify", form)
        saved = await ui.post(f"/ui/companies/{qualified.id}/save")
        again = await ui.post(f"/ui/companies/{qualified.id}/save")
        detail = await ui.get(saved.headers["location"])

    assert saved.status_code == 303
    assert saved.headers["location"].endswith("notice=prospect_saved")
    assert again.headers["location"].endswith("notice=prospect_exists")
    assert "Justifying qualification" in detail.text and "Good Co" in detail.text
    assert _count(db_session_factory, SavedProspect) == 1


async def test_preliminary_matches_explain_why_they_cannot_be_saved(
    ui_factory: Any, db_session: Session
) -> None:
    # Search-result data only (no website research): at most preliminary.
    candidate = make_candidate(db_session, research_text=None)
    form = {
        "profile": "freight",
        "target_keywords": "saas",
        "target_locations": "",
        "target_tlds": "",
        "excluded_keywords": "",
        "threshold": "30",
        "min_evidence_coverage": "0.5",
    }
    async with ui_factory() as ui:
        await ui.post(f"/ui/companies/{candidate.id}/qualify", form)
        page = await ui.get(f"/ui/companies/{candidate.id}")
        refused = await ui.post(f"/ui/companies/{candidate.id}/save")

    assert "Preliminary match" in page.text
    assert "Preliminary matches cannot be saved." in page.text
    assert "Save as prospect" not in page.text
    assert refused.status_code == 409


# -- saved prospects ------------------------------------------------------------------------
async def test_prospect_list_filters_and_fragments(ui_factory: Any, db_session: Session) -> None:
    make_saved_prospect(db_session, name="Acme Logistics", domain="acme.com")
    make_saved_prospect(db_session, name="Beta Freight", domain="beta.com")
    async with ui_factory() as ui:
        everything = await ui.get("/ui/prospects")
        filtered = await ui.get("/ui/prospects", params={"q": "beta"})
        fragment = await ui.get(
            "/ui/prospects",
            params={"domain": "acme.com"},
            headers={"HX-Request": "true", "HX-Target": "prospect-results"},
        )
        invalid = await ui.get("/ui/prospects", params={"min_score": "90", "max_score": "10"})
        no_match = await ui.get("/ui/prospects", params={"q": "zzz"})

    assert "Acme Logistics" in everything.text and "Beta Freight" in everything.text
    assert "Beta Freight" in filtered.text and "Acme Logistics" not in filtered.text
    assert fragment.text.lstrip().startswith('<section id="prospect-results"')
    assert "Acme Logistics" in fragment.text and "Beta Freight" not in fragment.text
    assert invalid.status_code == 422 and "min_score must not exceed max_score" in invalid.text
    assert "No matching prospects" in no_match.text


async def test_prospect_detail_shows_research_history(ui_factory: Any, db_session: Session) -> None:
    prospect = make_saved_prospect(db_session)
    async with ui_factory() as ui:
        page = await ui.get(f"/ui/prospects/{prospect.id}")
        missing = await ui.get("/ui/prospects/9999")

    assert page.status_code == 200
    assert "Research history" in page.text and "Success" in page.text
    assert "It is not recalculated." in page.text
    assert missing.status_code == 404


async def test_failure_category_text_is_used(ui_factory: Any, db_session: Session) -> None:
    candidate = make_candidate(db_session, research_text=None)
    fetcher = FakeWebsiteFetcher(
        results={
            candidate.website_url: FetchResult(
                requested_url=candidate.website_url,
                fetched_at=FAKE_FETCHED_AT,
                outcome=FetchOutcome.FAILED,
                fetcher="fake",
                failure=FetchFailure.BLOCKED_DESTINATION,
            )
        }
    )
    async with ui_factory(fetcher=fetcher) as ui:
        await ui.post(f"/ui/companies/{candidate.id}/research", {"refresh": "false"})
        page = await ui.get(f"/ui/companies/{candidate.id}")
    assert "The address points to a private or blocked network." in page.text
