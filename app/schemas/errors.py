"""Error response schemas shared by every endpoint."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """Machine-readable error description."""

    code: str = Field(
        description="Stable, snake_case error code for programmatic handling.",
        examples=["not_found"],
    )
    message: str = Field(
        description="Human-readable message safe to display.",
        examples=["Company not found."],
    )
    details: Any = Field(
        default=None,
        description=(
            "Optional structured context. For validation errors this is the "
            "list of field errors produced by FastAPI."
        ),
    )


class ErrorResponse(BaseModel):
    """Envelope used for every non-2xx response."""

    error: ErrorDetail
