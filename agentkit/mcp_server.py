"""MCP server generated from a Catalogue. No logging of arguments, no network at request time.

from agentkit.mcp_server import build_server, run_cli
run_cli(CATALOGUE)          # --transport stdio|http --allowed-host ...
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as McpToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from .catalogue import Catalogue, ToolError, ToolSpec, validation_detail
from .metrics import METRICS

MAX_RESPONSE_BYTES = 256_000
OnCall = Callable[[str, int], Awaitable[None]]


def _tool_function(spec: ToolSpec, run: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]):
    """Build an async function whose signature mirrors the Pydantic input model, so the SDK derives the
    JSON schema (types, defaults, descriptions) from the catalogue."""
    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for fname, f in spec.input_model.model_fields.items():
        hint = Annotated[f.annotation, Field(description=f.description or "")]
        default = inspect.Parameter.empty if f.is_required() else f.default
        params.append(inspect.Parameter(fname, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=hint))
        annotations[fname] = hint

    async def fn(**kwargs: Any) -> dict[str, Any]:
        return await run(spec.name, kwargs)

    fn.__name__ = spec.name
    fn.__qualname__ = spec.name
    fn.__doc__ = spec.description
    fn.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])  # type: ignore[attr-defined]
    annotations["return"] = dict[str, Any]
    fn.__annotations__ = annotations
    return fn


def build_server(cat: Catalogue, *, on_call: OnCall | None = None) -> MCPServer:
    """`on_call(tool_name, count)` is awaited after each successful call (billing); count = events to charge."""
    info = cat.info
    mcp = MCPServer(
        name=info.name,
        title=info.title,
        instructions=info.instructions,
        version=info.version,
        website_url=info.website,
        log_level=os.environ.get(f"{info.env_prefix}_LOG_LEVEL", "WARNING"),  # type: ignore[arg-type]
    )
    ro = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    async def run(name: str, params: dict[str, Any]) -> dict[str, Any]:
        spec = cat.by_name[name]
        params = {k: v for k, v in params.items() if k in spec.input_model.model_fields}
        with METRICS.observe(name) as m:
            try:
                env = cat.call(name, params)
            except ValidationError as e:
                m.status = "invalid_input"
                raise McpToolError(f"invalid input: {validation_detail(e)}") from None
            except ToolError as e:
                m.status = "invalid_input"
                raise McpToolError(str(e)) from None
            out = env.model_dump(mode="json")
            if len(json.dumps(out)) > MAX_RESPONSE_BYTES:
                m.status = "truncated"
                for k in spec.truncate_keys:
                    v = out["result"].get(k)
                    if isinstance(v, list) and len(v) > 50:
                        out["result"][k] = v[:50]
                        out["warnings"].append(f"{k} truncated to 50 items")
            if on_call is not None:
                await on_call(name, cat.count_for(name, env))
            return out

    for spec in cat.tools:
        mcp.tool(name=spec.name, title=spec.title, description=spec.description, annotations=ro)(
            _tool_function(spec, run)
        )

    @mcp.custom_route("/", methods=["GET"])
    async def root(_request):  # readiness probe for hosted platforms (Apify standby); no data
        from starlette.responses import JSONResponse

        return JSONResponse({"service": info.name, "mcp": "/mcp", "data_version": cat.data_version()})

    @mcp.resource(f"{info.name}://data-version", title="Data version", description="Current data/rules version.")
    def data_version() -> str:
        return cat.data_version()

    @mcp.resource(
        f"{info.name}://metrics",
        title="Aggregate metrics",
        description="Aggregate counters only (tool, status, latency bucket). No queries are logged.",
    )
    def metrics() -> str:
        return json.dumps(METRICS.snapshot(), indent=1)

    return mcp


def run_cli(cat: Catalogue, argv: list[str] | None = None, *, default_port: int = 8765) -> None:
    info = cat.info
    ap = argparse.ArgumentParser(prog=f"{info.name}-mcp", description=f"{info.title} MCP server")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=default_port)
    ap.add_argument("--path", default="/mcp")
    ap.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="public hostname(s) accepted in the Host header (DNS-rebinding protection). Localhost always allowed.",
    )
    ns = ap.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get(f"{info.env_prefix}_LOG_LEVEL", "WARNING"),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    server = build_server(cat)
    if ns.transport == "stdio":
        server.run("stdio")
        return

    import anyio
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = {"127.0.0.1", "localhost", f"127.0.0.1:{ns.port}", f"localhost:{ns.port}", f"{ns.host}:{ns.port}"}
    for h in ns.allowed_host:
        if h:
            hosts.update({h, f"{h}:443", f"{h}:{ns.port}"})
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(
            {f"https://{h}" for h in ns.allowed_host if h}
            | {f"http://127.0.0.1:{ns.port}", f"http://localhost:{ns.port}"}
        ),
    )
    anyio.run(
        lambda: server.run_streamable_http_async(
            host=ns.host,
            port=ns.port,
            streamable_http_path=ns.path,
            stateless_http=True,
            json_response=True,
            transport_security=security,
        )
    )
