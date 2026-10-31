"""REST mirror of the catalogue (FastAPI). Privacy: no access log, no bodies, no IPs persisted; aggregate counters only.
Rate limiting: in-memory token bucket keyed by X-API-Key when present, else client address — memory only."""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .catalogue import Catalogue, ToolError
from .envelope import Envelope
from .metrics import METRICS

MAX_BODY_BYTES = 64 * 1024


class TokenBucket:
    def __init__(self, rate_per_minute: int, burst: int) -> None:
        self.rate = rate_per_minute / 60.0
        self.burst = burst
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1:
                self._state[key] = (tokens, now)
                return False
            self._state[key] = (tokens - 1, now)
            if len(self._state) > 50_000:
                stale = [k for k, (_, t) in self._state.items() if now - t > 300]
                for k in stale:
                    self._state.pop(k, None)
            return True


def create_app(cat: Catalogue, *, rate_per_minute: int | None = None, burst: int | None = None) -> FastAPI:
    info = cat.info
    p = info.env_prefix
    limiter = TokenBucket(
        rate_per_minute if rate_per_minute is not None else int(os.environ.get(f"{p}_RATE_PER_MINUTE", "120")),
        burst if burst is not None else int(os.environ.get(f"{p}_RATE_BURST", "60")),
    )
    app = FastAPI(
        title=f"{info.title} API",
        version=cat.data_version(),
        description=info.description,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            return JSONResponse({"error": "request body too large"}, status_code=413)
        if request.url.path.startswith("/v1/tools/"):
            key = request.headers.get("x-api-key") or (request.client.host if request.client else "anon")
            if not limiter.allow(key):
                return JSONResponse({"error": "rate limit exceeded"}, status_code=429, headers={"Retry-After": "10"})
        return await call_next(request)

    @app.get("/v1/health", tags=["meta"])
    def health() -> dict[str, Any]:
        return {"status": "ok", "service": info.name, "data_version": cat.data_version(), **cat.health_extra()}

    @app.get("/v1/tools", tags=["meta"])
    def tools() -> dict[str, Any]:
        return {
            "tools": [
                {
                    "name": t.name,
                    "title": t.title,
                    "description": t.description,
                    "input_schema": t.input_model.model_json_schema(),
                    "billing_event": t.event,
                }
                for t in cat.tools
            ]
        }

    @app.get("/v1/metrics", tags=["meta"])
    def metrics() -> dict[str, Any]:
        return METRICS.snapshot()

    def make_endpoint(name: str, model: type):
        def endpoint(body: model) -> Envelope:  # type: ignore[valid-type]
            with METRICS.observe(name) as m:
                try:
                    return cat.by_name[name].handler(body)
                except ValidationError as e:
                    m.status = "invalid_input"
                    raise HTTPException(422, detail=e.errors(include_url=False)) from None
                except ToolError as e:
                    m.status = "invalid_input"
                    raise HTTPException(422, detail=str(e)) from None

        endpoint.__name__ = name
        endpoint.__annotations__ = {"body": model, "return": Envelope}
        return endpoint

    for spec in cat.tools:
        app.add_api_route(
            f"/v1/tools/{spec.name}",
            make_endpoint(spec.name, spec.input_model),
            methods=["POST"],
            response_model=Envelope,
            summary=spec.title,
            description=spec.description,
            tags=["tools"],
            name=spec.name,
        )
    return app


def run_cli(cat: Catalogue, argv: list[str] | None = None, *, default_port: int = 8766) -> None:
    import uvicorn

    ap = argparse.ArgumentParser(prog=f"{cat.info.name}-api")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=default_port)
    ns = ap.parse_args(argv)
    logging.getLogger("uvicorn.access").disabled = True
    uvicorn.run(create_app(cat), host=ns.host, port=ns.port, access_log=False, log_level="warning")
