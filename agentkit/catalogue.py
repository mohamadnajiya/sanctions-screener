"""Tool catalogue: the single source of truth for names, agent-facing descriptions, input schemas, handlers
and billing events. MCP and REST are generated from it, so they cannot drift."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from .envelope import Envelope


class ToolError(ValueError):
    """Raised by handlers for a well-formed but unanswerable request. Message is safe to show to the caller."""


Handler = Callable[[BaseModel], Envelope]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str  # written for the AI agent deciding whether to call us; no instructions beyond describing the tool
    input_model: type[BaseModel]
    handler: Handler
    event: str | None = None  # pay-per-event name; None = free everywhere
    truncate_keys: tuple[str, ...] = ()  # list fields that may be cut to 50 items if the response is oversized
    charge_count: Callable[[Envelope], int] | None = None  # events per call (batch tools); default 1; 0 = free


@dataclass(frozen=True)
class AgentInfo:
    name: str  # package / Actor name, e.g. "id-format-oracle"
    title: str  # human title
    instructions: str  # MCP server instructions (shown to the agent once)
    description: str  # REST/OpenAPI description
    version: str  # code version
    website: str = "https://noahsarch.tech"
    env_prefix: str = "AGENT"  # env vars: <PREFIX>_LOG_LEVEL, <PREFIX>_RATE_PER_MINUTE, ...


@dataclass
class Catalogue:
    info: AgentInfo
    tools: list[ToolSpec]
    data_version: Callable[[], str]  # agents with datasets return the dataset version; others return the code version
    health_extra: Callable[[], dict[str, Any]] = field(default_factory=lambda: dict)

    def __post_init__(self) -> None:
        names = [t.name for t in self.tools]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate tool names: {names}")
        self.by_name: dict[str, ToolSpec] = {t.name: t for t in self.tools}

    def call(self, name: str, params: dict[str, Any]) -> Envelope:
        """Validate `params` against the tool's model and run the handler. Raises ValidationError / ToolError."""
        spec = self.by_name[name]
        body = spec.input_model.model_validate(params)
        return spec.handler(body)

    def event_for(self, name: str) -> str | None:
        return self.by_name[name].event

    def count_for(self, name: str, env: Envelope) -> int:
        fn = self.by_name[name].charge_count
        return max(0, int(fn(env))) if fn else 1  # 0 = nothing billable happened (e.g. fetch failed) → no charge


def validation_detail(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
