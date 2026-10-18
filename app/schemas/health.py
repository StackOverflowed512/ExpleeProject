"""Health check response schema."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

HealthStatus = Literal["ok"]


class HealthResponse(BaseModel):
    """Response body for ``GET /health``.

    This endpoint reports that the application process is serving requests.
    It does not verify downstream dependencies such as the database.
    """

    status: HealthStatus = Field(
        description="Service status. `ok` means the process is serving requests.",
        examples=["ok"],
    )
    service: str = Field(
        description="Name of the running service.",
        examples=["explee"],
    )
    environment: str = Field(
        description="Application environment the process is running in.",
        examples=["development"],
    )
    version: str = Field(
        description="Application version.",
        examples=["0.1.0"],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status": "ok",
                    "service": "explee",
                    "environment": "development",
                    "version": "0.1.0",
                }
            ]
        }
    }
