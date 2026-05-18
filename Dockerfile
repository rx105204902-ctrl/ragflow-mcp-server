FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    RAGFLOW_MCP_HOST=0.0.0.0 \
    RAGFLOW_MCP_PORT=9388 \
    RAGFLOW_MCP_BASE_URL=http://127.0.0.1:9380 \
    RAGFLOW_MCP_LAUNCH_MODE=self-host

WORKDIR /app

COPY requirements.txt ./

RUN uv pip install --system --no-cache -r requirements.txt

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && chown app:app /app

COPY --chown=app:app server ./server
COPY --chown=app:app docker-entrypoint.sh /usr/local/bin/ragflow-mcp-entrypoint

RUN chmod +x /usr/local/bin/ragflow-mcp-entrypoint

USER app

EXPOSE 9388

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,socket; s=socket.create_connection(('127.0.0.1', int(os.environ.get('RAGFLOW_MCP_PORT', '9388'))), 3); s.close()"

ENTRYPOINT ["ragflow-mcp-entrypoint"]
