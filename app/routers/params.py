"""Shared, validated path parameters for routers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Path

MAX_DB_ID = 2_147_483_647

CandidateId = Annotated[
    int,
    Path(ge=1, le=MAX_DB_ID, description="Persisted company candidate ID."),
]
ProspectId = Annotated[
    int,
    Path(ge=1, le=MAX_DB_ID, description="Saved prospect ID."),
]
