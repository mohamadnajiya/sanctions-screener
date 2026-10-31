"""Reusable contract checks. Each agent's tests/test_contract.py calls these with its catalogue."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi.testclient import TestClient

from .catalogue import Catalogue
from .mcp_server import build_server
from .rest import create_app


def check_mcp_matches_catalogue(cat: Catalogue) -> None:
    from mcp import Client

    async def go() -> None:
        async with Client(build_server(cat)) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            assert set(tools) == set(cat.by_name), set(tools) ^ set(cat.by_name)
            for spec in cat.tools:
                schema = tools[spec.name].input_schema
                assert set(schema["properties"]) == set(spec.input_model.model_fields), spec.name
                assert tools[spec.name].description == spec.description

    asyncio.run(go())


def check_rest_matches_catalogue(cat: Catalogue) -> None:
    app = create_app(cat, rate_per_minute=100000, burst=100000)
    with TestClient(app) as tc:
        r = tc.get("/v1/tools")
        assert r.status_code == 200
        assert {t["name"] for t in r.json()["tools"]} == set(cat.by_name)
        paths = tc.get("/openapi.json").json()["paths"]
        for spec in cat.tools:
            assert f"/v1/tools/{spec.name}" in paths
        h = tc.get("/v1/health").json()
        assert h["status"] == "ok" and h["data_version"] == cat.data_version()


def check_parity(cat: Catalogue, examples: dict[str, dict[str, Any]]) -> None:
    """Same input through MCP and REST → same envelope (minus nothing; both are the handler)."""
    from mcp import Client

    app = create_app(cat, rate_per_minute=100000, burst=100000)

    async def via_mcp() -> dict[str, dict[str, Any]]:
        out = {}
        async with Client(build_server(cat)) as c:
            for name, params in examples.items():
                r = await c.call_tool(name, params)
                out[name] = r.structured_content
        return out

    mcp_out = asyncio.run(via_mcp())
    with TestClient(app) as tc:
        for name, params in examples.items():
            r = tc.post(f"/v1/tools/{name}", json=params)
            assert r.status_code == 200, (name, r.text)
            assert r.json() == mcp_out[name], name


def check_no_request_logging(cat: Catalogue, name: str, params: dict[str, Any], marker: str, caplog) -> None:
    """Run a call with a distinctive marker in the arguments and assert it never reaches any log record."""
    caplog.set_level(logging.DEBUG)
    app = create_app(cat, rate_per_minute=100000, burst=100000)
    with TestClient(app) as tc:
        tc.post(f"/v1/tools/{name}", json=params)
    assert marker not in caplog.text
    assert marker not in json.dumps(__import__("agentkit.metrics").metrics.METRICS.snapshot())


def check_p95_latency(cat: Catalogue, name: str, params: dict[str, Any], *, budget_ms: float, n: int = 200) -> float:
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        cat.call(name, params)
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    p95 = times[int(0.95 * len(times)) - 1]
    assert p95 <= budget_ms, f"{name} p95 {p95:.2f}ms > {budget_ms}ms"
    return p95


def check_error_shapes(cat: Catalogue, name: str, bad_params: dict[str, Any]) -> None:
    """Invalid input → REST 422 with no stack trace; MCP ToolError with a short message."""
    from mcp import Client

    app = create_app(cat, rate_per_minute=100000, burst=100000)
    with TestClient(app) as tc:
        r = tc.post(f"/v1/tools/{name}", json=bad_params)
        assert r.status_code == 422, r.text
        assert "Traceback" not in r.text

    async def go() -> None:
        async with Client(build_server(cat)) as c:
            r = await c.call_tool(name, bad_params)
            assert r.is_error
            assert "Traceback" not in (r.content[0].text if r.content else "")

    asyncio.run(go())
