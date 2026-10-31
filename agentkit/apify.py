"""Apify Standby entry point + pay-per-event hook. `apify` is an optional extra used only in the Actor image.

from agentkit.apify import run_standby
def main(): run_standby(CATALOGUE)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from .catalogue import Catalogue
from .mcp_server import build_server

log = logging.getLogger("agentkit.apify")


def make_on_call(cat: Catalogue):
    from apify import Actor

    async def on_call(tool: str, count: int = 1) -> None:
        event = cat.event_for(tool)
        if event is None or count <= 0:
            return
        try:
            await Actor.charge(event_name=event, count=count)
        except Exception as e:  # noqa: BLE001 - billing must never fail the call
            log.warning("charge failed for event %s: %s", event, type(e).__name__)

    return on_call


async def _serve(cat: Catalogue) -> None:
    from apify import Actor
    from mcp.server.transport_security import TransportSecuritySettings

    async with Actor:
        port = int(os.environ.get("ACTOR_WEB_SERVER_PORT") or Actor.configuration.web_server_port or 4321)
        Actor.log.info("%s MCP starting on port %s", cat.info.title, port)
        server = build_server(cat, on_call=make_on_call(cat))
        # Apify terminates TLS and authenticates in front of us; Host headers are platform-controlled.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        await server.run_streamable_http_async(
            host="0.0.0.0",  # noqa: S104 - inside the Actor container
            port=port,
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
            transport_security=security,
        )


def is_standby_run() -> bool:
    """Apify starts Standby runs with APIFY_META_ORIGIN=STANDBY; ordinary 'Start' runs carry WEB/API/SCHEDULER/..."""
    return os.environ.get("APIFY_META_ORIGIN", "").upper() == "STANDBY"


def run_standby(cat: Catalogue) -> None:
    logging.basicConfig(level=os.environ.get(f"{cat.info.env_prefix}_LOG_LEVEL", "WARNING"), stream=sys.stderr)
    asyncio.run(_serve(cat))


def run_actor(cat: Catalogue, batch: Callable[[Any, dict[str, Any]], Awaitable[None]] | None = None) -> None:
    """Entry point for Actors that serve MCP in Standby *and* accept an ordinary run with input.
    `batch(Actor, input)` handles the ordinary run: it should push results to the default dataset and call
    `await Actor.charge(event_name=..., count=...)` itself."""
    logging.basicConfig(level=os.environ.get(f"{cat.info.env_prefix}_LOG_LEVEL", "WARNING"), stream=sys.stderr)
    if batch is None or is_standby_run():
        asyncio.run(_serve(cat))
        return

    async def _batch() -> None:
        from apify import Actor

        async with Actor:
            inp = await Actor.get_input() or {}
            await batch(Actor, inp)

    asyncio.run(_batch())
