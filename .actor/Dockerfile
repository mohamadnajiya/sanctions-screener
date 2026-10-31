# Apify Standby Actor image for the GCC Sanctions & Terror-List Screener MCP server (same hardened build + the Apify SDK for pay-per-event).
FROM python:3.12-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY deploy/requirements.txt ./requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --no-deps -r requirements.txt \
 && /opt/venv/bin/pip install "apify>=4.2,<5"
COPY pyproject.toml README.md ./
COPY screen ./screen
COPY agentkit ./agentkit

COPY data ./data
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist . && /opt/venv/bin/pip install --no-deps /dist/*.whl \
 && (/opt/venv/bin/pip uninstall -y pip setuptools 2>/dev/null || true)

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DO_NOT_TRACK=1 SCREEN_DATA_DIR=/app/data SCREEN_LOG_LEVEL=INFO PATH="/opt/venv/bin:$PATH"
RUN groupadd -r agent && useradd -r -g agent -d /app -s /usr/sbin/nologin agent
COPY --from=build /opt/venv /opt/venv
COPY --chown=agent:agent data /app/data
WORKDIR /app
USER agent
CMD ["screen-mcp-apify"]
