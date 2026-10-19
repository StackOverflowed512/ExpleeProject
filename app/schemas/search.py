"""Provider-agnostic normalized search result."""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class SearchResultItem(BaseModel):
    """One normalized web search result.

    Only values supplied by the provider are populated. Missing optional
    values stay ``None``; the application never infers or invents company
    names, descriptions, domains, or contact details.
    """

    title: str = Field(
        min_length=1,
        max_length=300,
        description="Result or company title as supplied by the provider.",
    )
    url: HttpUrl = Field(
        description="Result URL. Only http and https URLs are accepted.",
    )
    snippet: str | None = Field(
        default=None,
        description="Provider-supplied description or snippet, if any.",
    )
    provider: str = Field(
        min_length=1,
        max_length=50,
        description="Name of the provider that produced this result.",
    )
    provider_result_id: str | None = Field(
        default=None,
        max_length=255,
        description="Provider-specific stable identifier, when supplied.",
    )
