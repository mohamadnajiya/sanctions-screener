# GCC Sanctions & Terror-List Screener — slim, non-root, read-only-friendly image. One image, two commands:
#   screen-mcp --transport http   |   screen-api
# Nothing in this image phones home: no telemetry, no update checks, no network at request time.
FROM python:3.12-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY deploy/requirements.txt ./requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --no-deps -r requirements.txt
COPY pyproject.toml README.md ./
COPY screen ./screen
COPY agentkit ./agentkit

COPY data ./data
RUN pip install --no-cache-dir build \
 && python -m build --wheel --outdir /dist . \
 && /opt/venv/bin/pip install --no-deps /dist/*.whl \
 && (/opt/venv/bin/pip uninstall -y pip setuptools 2>/dev/null || true)

FROM python:3.12-slim
LABEL org.opencontainers.image.title="GCC Sanctions & Terror-List Screener" \
      org.opencontainers.image.vendor="Noah's Arch" \
      org.opencontainers.image.description="OFAC, UN, EU, UK and UAE sanctions screening with Arabic-aware matching — MCP + REST"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DO_NOT_TRACK=1 \
    SCREEN_DATA_DIR=/app/data SCREEN_LOG_LEVEL=WARNING PATH="/opt/venv/bin:$PATH"
RUN groupadd -r agent && useradd -r -g agent -d /app -s /usr/sbin/nologin agent
COPY --from=build /opt/venv /opt/venv
COPY --chown=agent:agent data /app/data
WORKDIR /app
USER agent
EXPOSE 8785 8786
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import socket,os; s=socket.create_connection(('127.0.0.1', int(os.environ.get('SCREEN_HEALTH_PORT','8785'))),3); s.close()" || exit 1
CMD ["screen-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8785"]
