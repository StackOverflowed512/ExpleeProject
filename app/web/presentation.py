"""Human-readable labels and display hints for templates.

Everything here is presentation: wording, a tone used for styling (always
paired with a text label, never color alone), and which buttons a page
shows. The services remain the source of truth. A hidden button is a
convenience, not a safeguard: every action is re-checked by the backend
when it is submitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings
from app.schemas.drafts import DraftDetail
from app.services.approvals import approval_matches
from app.services.delivery import STALE_SENDING_AFTER
from app.services.providers.email import EmailProvider

Tone = str  # "neutral" | "info" | "success" | "warning" | "danger"


@dataclass(frozen=True)
class StatusView:
    label: str
    tone: Tone
    hint: str = ""


_STATUSES: dict[str, dict[str, StatusView]] = {
    "draft_state": {
        "pending": StatusView("Awaiting approval", "neutral", "Review it, then approve it."),
        "approved": StatusView(
            "Approved, not sent", "info", "Approved for one recipient; nothing sent yet."
        ),
        "sending": StatusView("Sending", "warning", "A delivery attempt is in progress."),
        "sent": StatusView("Sent", "success", "The mail server accepted the message."),
        "delivery_uncertain": StatusView(
            "Delivery uncertain",
            "danger",
            "The message may have been delivered. Check before doing anything else.",
        ),
    },
    "attempt_outcome": {
        "in_progress": StatusView("In progress", "warning"),
        "dry_run": StatusView("Dry run (simulated)", "neutral", "No email was sent."),
        "sent": StatusView("Sent", "success"),
        "failed": StatusView("Failed", "danger"),
        "uncertain": StatusView(
            "Uncertain", "warning", "The server may have accepted the message."
        ),
    },
    "attempt_mode": {
        "dry_run": StatusView("Dry run", "neutral", "Simulated; no mail server contacted."),
        "real": StatusView("Real delivery", "info", "Sent to the mail server."),
        "manual_resolution": StatusView(
            "Manual resolution", "neutral", "An operator recorded the verified outcome."
        ),
    },
    "research_status": {
        "available": StatusView("Researched", "success", "Usable website content is stored."),
        "partial": StatusView(
            "Partially researched", "warning", "Only part of the page could be used."
        ),
        "unavailable": StatusView(
            "Research failed", "danger", "No usable website content is stored."
        ),
        "not_researched": StatusView(
            "Not researched", "neutral", "The website has not been fetched yet."
        ),
    },
    "research_outcome": {
        "success": StatusView("Success", "success"),
        "partial": StatusView("Partial", "warning"),
        "empty": StatusView("No usable text", "warning"),
        "failed": StatusView("Failed", "danger"),
    },
    "qualification_status": {
        "qualified": StatusView(
            "Qualified", "success", "Meets the threshold with website evidence."
        ),
        "preliminary": StatusView(
            "Preliminary match",
            "warning",
            "Meets the threshold on search-result data only; research the website "
            "to confirm. Preliminary matches cannot be saved.",
        ),
        "unqualified": StatusView("Not qualified", "danger"),
        "insufficient_evidence": StatusView(
            "Insufficient evidence", "neutral", "Too little sourced data to evaluate."
        ),
    },
    "factor_result": {
        "matched": StatusView("Matched", "success"),
        "not_matched": StatusView("Not matched", "danger"),
        "unknown": StatusView("Unknown", "neutral"),
        "not_applicable": StatusView("Not applicable", "neutral"),
    },
}

# Sanitized failure categories (research and delivery) in plain language.
FAILURE_CATEGORIES: dict[str, str] = {
    "invalid_url": "The stored website address is not valid.",
    "disallowed_scheme": "Only http and https websites can be researched.",
    "credentials_in_url": "Website addresses with credentials are refused.",
    "disallowed_port": "The website uses a port that is not allowed.",
    "blocked_destination": "The address points to a private or blocked network.",
    "dns_resolution_failed": "The website's domain could not be resolved.",
    "timeout": "The website did not respond in time.",
    "connection_failed": "The website could not be reached.",
    "too_many_redirects": "The website redirected too many times.",
    "invalid_redirect": "The website redirected to an address that is not allowed.",
    "http_error": "The website returned an HTTP error.",
    "unsupported_content_type": "The website did not return an HTML page.",
    "malformed_response": "The website's response could not be read.",
    "email_connection_failed": "The mail server could not be reached. Nothing was sent.",
    "email_tls_failed": "A secure connection to the mail server failed. Nothing was sent.",
    "email_authentication_failed": "The mail server refused the login. Nothing was sent.",
    "email_timeout": "The mail server did not respond in time. Nothing was sent.",
    "email_rejected": "The mail server refused the message. Nothing was delivered.",
    "email_delivery_failed": "Delivery failed. Nothing was delivered.",
    "email_not_configured": "Email delivery is not configured.",
    "email_delivery_uncertain": (
        "The connection failed after the message was transmitted, so it may have "
        "been accepted."
    ),
    "interrupted": "The sending process stopped before recording a result.",
    "confirmed_not_delivered": "An operator confirmed the message was not delivered.",
}


def status_view(kind: str, value: str | None) -> StatusView:
    if value is None:
        return StatusView("None", "neutral")
    known = _STATUSES.get(kind, {}).get(value)
    return known or StatusView(value.replace("_", " ").capitalize(), "neutral")


def failure_text(category: str | None) -> str:
    if not category:
        return ""
    return FAILURE_CATEGORIES.get(category, category.replace("_", " ").capitalize() + ".")


@dataclass(frozen=True)
class DeliveryModeView:
    """What a real send would do with the current configuration."""

    real_enabled: bool
    title: str
    explanation: str
    blockers: tuple[str, ...]


def delivery_mode(settings: Settings, provider: EmailProvider) -> DeliveryModeView:
    blockers: list[str] = []
    if settings.email_dry_run:
        blockers.append("EMAIL_DRY_RUN is true, so only dry runs are allowed.")
    if not getattr(provider, "delivers_email", False):
        blockers.append(
            f"The email provider ({provider.name}) only simulates delivery; "
            "set EMAIL_PROVIDER=smtp for real email."
        )
    if not settings.email_sender:
        blockers.append("EMAIL_SENDER is not configured.")
    if blockers:
        return DeliveryModeView(
            real_enabled=False,
            title="Dry-run mode: real email is disabled",
            explanation=(
                "A dry run records a simulated attempt in the audit trail. It never "
                "contacts a mail server and nothing reaches the recipient."
            ),
            blockers=tuple(blockers),
        )
    return DeliveryModeView(
        real_enabled=True,
        title="Real delivery is enabled",
        explanation=(
            f"Sending hands one email to the configured {provider.name} mail server "
            "from the configured sender. Dry runs remain available and send nothing."
        ),
        blockers=(),
    )


@dataclass(frozen=True)
class DraftActions:
    """Which controls the review page offers (display hints only)."""

    can_approve: bool
    approval_matches: bool | None
    can_send: bool
    can_resolve: bool
    resolve_wait: bool
    in_progress_since: datetime | None


def draft_actions(detail: DraftDetail, now: datetime | None = None) -> DraftActions:
    current = now or datetime.now(UTC)
    # The same check the delivery service runs; it only reads attributes that
    # the API records share with the ORM rows.
    matches = (
        approval_matches(detail.approval, detail)  # type: ignore[arg-type]
        if detail.approval is not None
        else None
    )
    claim = next(
        (a for a in reversed(detail.send_attempts) if a.outcome == "in_progress"), None
    )
    stale = claim is None or current - claim.started_at >= STALE_SENDING_AFTER
    return DraftActions(
        can_approve=detail.state == "pending" and detail.approval is None,
        approval_matches=matches,
        can_send=detail.state == "approved" and bool(matches),
        can_resolve=detail.state == "delivery_uncertain"
        or (detail.state == "sending" and stale),
        resolve_wait=detail.state == "sending" and not stale,
        in_progress_since=claim.started_at if claim is not None else None,
    )


# Fixed notice messages shown after a redirect (``?notice=<code>``). Only
# these codes are honoured, so a crafted link cannot inject text.
NOTICES: dict[str, StatusView] = {
    "research_success": StatusView("Website research finished: usable content stored.", "success"),
    "research_partial": StatusView(
        "Website research finished with partial content.", "warning"
    ),
    "research_empty": StatusView(
        "The website was fetched but contained no usable text.", "warning"
    ),
    "research_failed": StatusView(
        "Website research failed. Earlier usable research, if any, is kept.", "danger"
    ),
    "research_reused": StatusView(
        "Fresh research already existed, so nothing was fetched. Use Refresh to fetch again.",
        "info",
    ),
    "research_done": StatusView(
        "Website research finished. See the latest attempt below.", "info"
    ),
    "qualified": StatusView("Qualification recorded.", "success"),
    "prospect_saved": StatusView("Saved as a prospect.", "success"),
    "prospect_exists": StatusView("This company was already saved; nothing changed.", "info"),
    "campaign_created": StatusView("Campaign created. Generate drafts when ready.", "success"),
    "drafts_generated": StatusView("Draft generation finished.", "success"),
    "drafts_remaining": StatusView(
        "Draft generation finished for this batch. More prospects remain; generate again.",
        "info",
    ),
    "approved": StatusView(
        "Approval recorded for this exact draft and recipient. Nothing has been sent.",
        "success",
    ),
    "approval_exists": StatusView("This approval was already recorded; nothing changed.", "info"),
    "dry_run": StatusView(
        "Dry run recorded. No email was sent and no mail server was contacted.", "info"
    ),
    "sent": StatusView("The mail server accepted the message.", "success"),
    "already_sent": StatusView("This draft was already sent; it was not sent again.", "info"),
    "delivery_uncertain": StatusView(
        "Delivery is uncertain: the message may have been accepted. Do not send again "
        "until you have checked.",
        "danger",
    ),
    "resolved_delivered": StatusView("Recorded as delivered.", "success"),
    "resolved_not_delivered": StatusView(
        "Recorded as not delivered. The draft is approved again and may be sent.", "info"
    ),
}


def notice_for(code: str | None) -> StatusView | None:
    return NOTICES.get(code) if code else None


def research_notice(outcome: str, *, reused: bool) -> str:
    """Notice code after a research request; unknown outcomes get a generic one."""
    if reused:
        return "research_reused"
    code = f"research_{outcome}"
    return code if code in NOTICES else "research_done"


@dataclass(frozen=True)
class NextStep:
    """A suggested action on the overview page, derived from real counts."""

    label: str
    detail: str
    href: str
    tone: Tone


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def next_steps(
    *,
    candidates: int,
    prospects: int,
    campaigns: int,
    drafts_by_state: dict[str, int],
) -> list[NextStep]:
    """Suggested next actions, most urgent first. Only states that exist."""
    steps: list[NextStep] = []
    attention = drafts_by_state.get("delivery_uncertain", 0) + drafts_by_state.get("sending", 0)
    pending = drafts_by_state.get("pending", 0)
    approved = drafts_by_state.get("approved", 0)
    total_drafts = sum(drafts_by_state.values())
    if attention:
        steps.append(
            NextStep(
                f"Resolve {_plural(attention, 'delivery', 'deliveries')} needing attention",
                "Delivery is uncertain or was interrupted. Check before sending anything again.",
                "/ui/drafts?view=attention",
                "danger",
            )
        )
    if pending:
        steps.append(
            NextStep(
                f"Review {_plural(pending, 'draft')} awaiting approval",
                "Read each message and approve it for one recipient you have verified.",
                "/ui/drafts?view=review",
                "info",
            )
        )
    if approved:
        steps.append(
            NextStep(
                f"{_plural(approved, 'approved draft')} ready to dry-run or send",
                "Approved drafts are never sent until you choose to.",
                "/ui/drafts?view=approved",
                "neutral",
            )
        )
    if not candidates:
        steps.append(
            NextStep(
                "Discover your first companies",
                "Search for companies that match your target market.",
                "/ui/explorer",
                "neutral",
            )
        )
    elif not prospects:
        steps.append(
            NextStep(
                "Research and qualify discovered companies",
                "Only qualified companies can be saved as prospects.",
                "/ui/explorer",
                "neutral",
            )
        )
    elif not campaigns:
        steps.append(
            NextStep(
                "Create your first campaign",
                "Group saved prospects with a brief to generate drafts.",
                "/ui/campaigns/new",
                "neutral",
            )
        )
    elif not total_drafts:
        steps.append(
            NextStep(
                "Generate drafts for a campaign",
                "Drafts are created for review; generation never sends anything.",
                "/ui/campaigns",
                "neutral",
            )
        )
    return steps
