"""agentkit — the shared runtime for every Noah's Arch agent.

One tool catalogue (Pydantic input models + handlers) → MCP server (stdio / Streamable HTTP), REST mirror,
aggregate-only metrics, pay-per-event billing hook, Apify Standby entry point. Agents add logic and data;
nothing here logs request content or talks to the network at request time.

Vendored into each agent repo as `agentkit/` by `shared/tools/sync-agentkit.sh` so every repo stays self-contained
(Apify builds from a single repo). Source of truth: `shared/agentkit/`.
"""

from .catalogue import AgentInfo, Catalogue, ToolError, ToolSpec
from .envelope import Confidence, Envelope
from .metrics import METRICS, Metrics

__all__ = [
    "METRICS",
    "AgentInfo",
    "Catalogue",
    "Confidence",
    "Envelope",
    "Metrics",
    "ToolError",
    "ToolSpec",
]

__version__ = "0.2.2"
