#!/usr/bin/env sh
set -eu

: "${RAGFLOW_MCP_HOST:=0.0.0.0}"
: "${RAGFLOW_MCP_PORT:=9388}"
: "${RAGFLOW_MCP_BASE_URL:=http://127.0.0.1:9380}"
: "${RAGFLOW_MCP_LAUNCH_MODE:=self-host}"
: "${RAGFLOW_MCP_TRANSPORT_SSE_ENABLED:=true}"
: "${RAGFLOW_MCP_TRANSPORT_STREAMABLE_ENABLED:=true}"
: "${RAGFLOW_MCP_JSON_RESPONSE:=true}"

if [ -z "${RAGFLOW_MCP_HOST_API_KEY:-}" ]; then
  echo "RAGFLOW_MCP_HOST_API_KEY is required. Pass it at runtime with -e RAGFLOW_MCP_HOST_API_KEY=ragflow-..." >&2
  exit 64
fi

set -- python /app/server/server.py \
  --host="${RAGFLOW_MCP_HOST}" \
  --port="${RAGFLOW_MCP_PORT}" \
  --base-url="${RAGFLOW_MCP_BASE_URL}" \
  --mode="${RAGFLOW_MCP_LAUNCH_MODE}" \
  --api-key="${RAGFLOW_MCP_HOST_API_KEY}" \
  "$@"

exec "$@"
