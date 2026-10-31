"""The response envelope every tool returns. Same shape across all agents so client code is reusable."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Confidence = Literal["high", "medium", "low"]


class Envelope(BaseModel):
    result: dict[str, Any] = Field(..., description="Tool-specific result.")
    data_confidence: Confidence = Field(
        "high",
        description="high = deterministic / officially confirmed; medium = something estimated or a default assumed; "
        "low = format-only check with no authority behind it.",
    )
    estimated_items: list[str] = Field(default_factory=list, description="What, if anything, is estimated or assumed.")
    sources: list[str] = Field(default_factory=list, description="Public source URLs or standards behind the answer.")
    data_version: str = Field(..., description="Version of the data/rules the answer was produced from.")
    warnings: list[str] = Field(default_factory=list)
    disclaimer: str | None = Field(None, description="Present when the result must not be read as professional advice.")
