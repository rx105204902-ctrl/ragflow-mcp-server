# RAGFlow MCP Server

English | [Chinese](README.zh.md)

RAGFlow MCP Server exposes selected RAGFlow capabilities through the Model Context Protocol (MCP). It lets MCP clients list datasets, ingest documents, and retrieve knowledge from a configured RAGFlow backend through Streamable HTTP or SSE transports.

## Key Features

- Exposes RAGFlow dataset listing, document ingestion, and retrieval as MCP tools.
- Supports both Streamable HTTP (`/mcp`) and legacy SSE (`/sse`) transports.
- Supports `self-host` mode with one server-side API key and `host` mode with per-request credentials.
- Restricts document ingestion to trusted DeerFlow file capability URLs.
- Provides local, Docker, and Docker Compose startup paths.
- Keeps API keys out of the image and source-controlled files.

## Table of Contents

- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [MCP Endpoints](#mcp-endpoints)
- [MCP Tools](#mcp-tools)
- [Client Examples](#client-examples)
- [Docker](#docker)
- [Testing](#testing)
- [Architecture](#architecture)
- [Security](#security)
- [Troubleshooting](#troubleshooting)

## Tech Stack

| Area | Technology |
| --- | --- |
| Language | Python 3.12+ |
| MCP framework | FastMCP 3.x |
| MCP SDK | `mcp>=1.24.0` |
| HTTP client | HTTPX |
| ASGI server | Uvicorn |
| ASGI middleware | Starlette |
| CLI | Click |
| Validation | Pydantic |
| Environment loading | python-dotenv |
| Container runtime | Docker / Docker Compose |

## Project Structure

```text
.
|-- server/
|   `-- server.py                  # FastMCP server, RAGFlow connector, ASGI app, CLI entrypoint
|-- client/
|   |-- client.py                  # SSE client example
|   `-- streamable_http_client.py  # Streamable HTTP client example
|-- tests/
|   `-- test_document_ingest_file_uri.py
|-- Dockerfile
|-- docker-compose.yml
|-- docker-entrypoint.sh
|-- requirements.txt
|-- DOCKER.md
|-- README.md
`-- README.zh.md
```

## Prerequisites

For local development:

- Python 3.12 or newer.
- `uv` for the one-shot startup command, or `pip` with a virtual environment.
- A reachable RAGFlow backend.
- A RAGFlow API key when using `self-host` mode.

For Docker:

- Docker Engine or Docker Desktop.
- Docker Compose v2 when using `docker compose`.

## Quick Start

### Run with uv

Step 1: install `uv` if it is not already installed.

```bash
python -m pip install uv
```

Step 2: create the local environment and install dependencies.

```bash
uv venv .venv
uv pip install -r requirements.txt
```

Step 3: start the MCP server.

```bash
uv run python server/server.py \
  --host=0.0.0.0 \
  --port=9388 \
  --base-url=http://127.0.0.1:9380 \
  --mode=self-host \
  --api-key=ragflow-your-api-key
```

After startup, the server exposes:

- Streamable HTTP: `http://localhost:9388/mcp`
- SSE: `http://localhost:9388/sse`

### Run with a virtual environment

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

python server/server.py `
  --host=0.0.0.0 `
  --port=9388 `
  --base-url=http://127.0.0.1:9380 `
  --mode=self-host `
  --api-key=ragflow-your-api-key
```

Bash:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

python server/server.py \
  --host=0.0.0.0 \
  --port=9388 \
  --base-url=http://127.0.0.1:9380 \
  --mode=self-host \
  --api-key=ragflow-your-api-key
```

## Configuration

The server accepts CLI options and environment variables. The application loads `.env` and then lets environment variables override CLI values.

| CLI option | Environment variable | Code default | Docker default | Description |
| --- | --- | --- | --- | --- |
| `--host` | `RAGFLOW_MCP_HOST` | `127.0.0.1` | `0.0.0.0` | Bind address for the MCP server. |
| `--port` | `RAGFLOW_MCP_PORT` | `9382` | `9388` | Bind port for the MCP server. |
| `--base-url` | `RAGFLOW_MCP_BASE_URL` | `http://127.0.0.1:9380` | `http://127.0.0.1:9380` | RAGFlow backend base URL. |
| `--mode` | `RAGFLOW_MCP_LAUNCH_MODE` | `self-host` | `self-host` | Launch mode: `self-host` or `host`. |
| `--api-key` | `RAGFLOW_MCP_HOST_API_KEY` | empty | required | RAGFlow API key for `self-host` mode. |
| `--transport-sse-enabled` / `--no-transport-sse-enabled` | `RAGFLOW_MCP_TRANSPORT_SSE_ENABLED` | `true` | `true` | Enable or disable SSE transport. |
| `--transport-streamable-http-enabled` / `--no-transport-streamable-http-enabled` | `RAGFLOW_MCP_TRANSPORT_STREAMABLE_ENABLED` | `true` | `true` | Enable or disable Streamable HTTP transport. |
| `--json-response` / `--no-json-response` | `RAGFLOW_MCP_JSON_RESPONSE` | `true` | `true` | Use JSON responses for Streamable HTTP. |
| none | `RAGFLOW_MCP_FILE_URI_ALLOWED_BASE_URLS` | empty | empty | Comma-separated trusted DeerFlow file capability base URLs. |

### Launch Modes

`self-host` mode:

- The server starts with one RAGFlow API key.
- Every MCP request uses that server-side key when calling RAGFlow.
- `--api-key` or `RAGFLOW_MCP_HOST_API_KEY` is required.

`host` mode:

- The server acts as a multi-tenant gateway.
- Each HTTP request must include `Authorization: Bearer ...`, `api_key`, or `x-api-key`.
- The request token is forwarded to RAGFlow.

### Example `.env`

Do not commit `.env` files with real secrets.

```dotenv
RAGFLOW_MCP_HOST=0.0.0.0
RAGFLOW_MCP_PORT=9388
RAGFLOW_MCP_BASE_URL=http://127.0.0.1:9380
RAGFLOW_MCP_LAUNCH_MODE=self-host
RAGFLOW_MCP_HOST_API_KEY=ragflow-your-api-key
RAGFLOW_MCP_TRANSPORT_SSE_ENABLED=true
RAGFLOW_MCP_TRANSPORT_STREAMABLE_ENABLED=true
RAGFLOW_MCP_JSON_RESPONSE=true
```

## MCP Endpoints

| Endpoint | Transport | Default | Purpose |
| --- | --- | --- | --- |
| `/mcp` | Streamable HTTP | enabled | Primary endpoint for modern MCP clients. |
| `/sse` | SSE | enabled | Legacy SSE transport endpoint. |

When both transports are enabled, the server combines both route sets into one Starlette application.

## MCP Tools

### `list_datasets`

Lists RAGFlow datasets accessible to the active RAGFlow API key.

| Argument | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `page` | integer | no | `1` | Dataset page number. |
| `page_size` | integer | no | `30` | Number of datasets to return. Maximum is `1000`. |
| `id` | string | no | `null` | Optional dataset ID filter. |
| `name` | string | no | `null` | Optional dataset name filter. |

Example:

```json
{
  "page": 1,
  "page_size": 30
}
```

### `document_ingest`

Downloads files from trusted DeerFlow file capability URLs, uploads them to a RAGFlow dataset, and submits parsing tasks.

| Argument | Type | Required | Description |
| --- | --- | --- | --- |
| `dataset_id` | string | yes | Target RAGFlow dataset ID. |
| `files` | array | yes | File descriptors. Each item must include `file_uri`; `filename` is optional. |

Example:

```json
{
  "dataset_id": "dataset-1",
  "files": [
    {
      "file_uri": "https://gateway.example/api/file-capabilities/token"
    }
  ]
}
```

File URI validation rules:

- `file_uri` must use HTTP or HTTPS.
- Credentials, query strings, and fragments are rejected.
- Path traversal, local file paths, and arbitrary external URLs are rejected.
- If `RAGFLOW_MCP_FILE_URI_ALLOWED_BASE_URLS` is empty, the defaults are:
  - `http://localhost:8001/api/file-capabilities`
  - `http://127.0.0.1:8001/api/file-capabilities`
- Set `RAGFLOW_MCP_FILE_URI_ALLOWED_BASE_URLS` to allow a custom DeerFlow gateway.

Example:

```dotenv
RAGFLOW_MCP_FILE_URI_ALLOWED_BASE_URLS=https://gateway.example,https://files.example/api
```

### `retrieval`

Retrieves relevant chunks from RAGFlow for a question.

| Argument | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `question` | string | yes | none | Query text. |
| `dataset_ids` | array | no | `[]` | Dataset filter. If omitted, the server resolves accessible datasets. |
| `document_ids` | array | no | `[]` | Document filter. |
| `page` | integer | no | `1` | Result page number. |
| `page_size` | integer | no | `10` | Results per page. Maximum is `100`. |
| `similarity_threshold` | float | no | `0.2` | Minimum similarity threshold. |
| `vector_similarity_weight` | float | no | `0.3` | Vector similarity weight. |
| `keyword` | boolean | no | `false` | Enable keyword search. |
| `top_k` | integer | no | `1024` | Maximum candidates before ranking. |
| `rerank_id` | string | no | `null` | Optional reranking model ID. |
| `force_refresh` | boolean | no | `false` | Force metadata cache refresh. |

Example:

```json
{
  "dataset_ids": ["dataset-1"],
  "document_ids": [],
  "question": "How to install neovim?",
  "page": 1,
  "page_size": 10
}
```

## Client Examples

The `client/` directory contains two minimal examples.

Run the Streamable HTTP client:

```bash
python client/streamable_http_client.py
```

Run the SSE client:

```bash
python client/client.py
```

For `host` mode, include one of these headers in the client:

```python
headers = {"Authorization": "Bearer ragflow-your-api-key"}
```

```python
headers = {"api_key": "ragflow-your-api-key"}
```

The example clients currently use port `9382`. If you start the server with the Docker defaults in this repository, use port `9388`.

## Docker

More Docker details are available in [DOCKER.md](DOCKER.md).

Build the image:

```bash
docker build -t ragflow-mcp-server:latest .
```

Run with the default container port:

```bash
docker run --rm \
  --name ragflow-mcp-server \
  -p 9388:9388 \
  -e RAGFLOW_MCP_HOST_API_KEY=ragflow-your-api-key \
  ragflow-mcp-server:latest
```

If the RAGFlow backend runs on the host machine, Docker Desktop users should use `host.docker.internal`:

```bash
docker run --rm \
  --name ragflow-mcp-server \
  -p 9388:9388 \
  -e RAGFLOW_MCP_BASE_URL=http://host.docker.internal:9380 \
  -e RAGFLOW_MCP_HOST_API_KEY=ragflow-your-api-key \
  ragflow-mcp-server:latest
```

Run with Docker Compose:

```bash
export RAGFLOW_MCP_HOST_API_KEY=ragflow-your-api-key
export RAGFLOW_MCP_BASE_URL=http://host.docker.internal:9380
docker compose up --build
```

PowerShell:

```powershell
$env:RAGFLOW_MCP_HOST_API_KEY = "ragflow-your-api-key"
$env:RAGFLOW_MCP_BASE_URL = "http://host.docker.internal:9380"
docker compose up --build
```

Stop Compose services:

```bash
docker compose down
```

## Testing

Install runtime and test dependencies:

```bash
python -m pip install -r requirements.txt pytest pytest-asyncio
```

Run all tests:

```bash
pytest
```

Run the current focused test file:

```bash
pytest tests/test_document_ingest_file_uri.py
```

The tests use lightweight stubs for FastMCP internals, so the file URI validation and upload behavior can be tested without a running MCP server or RAGFlow backend.

## Architecture

### Startup Flow

1. Click parses CLI options in `server/server.py`.
2. `python-dotenv` loads `.env`.
3. Environment variables override CLI values.
4. FastMCP registers tools and lifespan context.
5. `create_starlette_app()` creates an ASGI app for `/mcp`, `/sse`, or both.
6. Uvicorn serves the ASGI app.

### RAGFlow Connector

`RAGFlowConnector` owns communication with RAGFlow:

- It maintains one async HTTPX client.
- It forwards bearer credentials to RAGFlow.
- It maps backend failures into MCP tool errors.
- It caches dataset and document metadata for retrieval mapping.
- It uploads document bytes with multipart form data.

### Authentication Flow

`self-host` mode:

```text
MCP client -> MCP server -> RAGFlow
                  uses startup API key
```

`host` mode:

```text
MCP client -> request auth header -> MCP server -> RAGFlow
```

### Document Ingestion Flow

```text
document_ingest
  -> validate dataset_id and files
  -> validate file_uri against allowed DeerFlow capability bases
  -> fetch file bytes from the DeerFlow gateway
  -> infer or validate the filename
  -> upload bytes to RAGFlow
  -> submit the parse task
  -> return accepted and submitted counts
```

### Retrieval Flow

```text
retrieval
  -> validate query and filters
  -> resolve accessible datasets when dataset_ids is empty
  -> load document metadata when needed
  -> call the RAGFlow retrieval API
  -> return MCP text content
```

## Security

- Never commit real RAGFlow API keys.
- Use `.env`, Docker Compose environment variables, Docker secrets, or deployment-platform secrets.
- `document_ingest` intentionally rejects arbitrary URLs and local paths.
- The Docker image runs as a non-root user.
- The Docker healthcheck only verifies that the TCP port accepts connections.
- If this service is exposed outside localhost, prefer `host` mode with per-request credentials and TLS in front of the service.

## Troubleshooting

### `--api-key is required when --mode is 'self-host'`

The server started in `self-host` mode without an API key.

Fix:

```bash
python server/server.py \
  --mode=self-host \
  --api-key=ragflow-your-api-key
```

or:

```bash
RAGFLOW_MCP_HOST_API_KEY=ragflow-your-api-key python server/server.py
```

### The container cannot reach `127.0.0.1:9380`

Inside a container, `127.0.0.1` points to the container, not the host machine.

Use:

```bash
-e RAGFLOW_MCP_BASE_URL=http://host.docker.internal:9380
```

On Linux, add:

```bash
--add-host=host.docker.internal:host-gateway
```

### `Missing or invalid authorization header`

The server is likely running in `host` mode and the client did not send credentials.

Use one of:

```text
Authorization: Bearer ragflow-your-api-key
```

```text
api_key: ragflow-your-api-key
```

### `file_uri is not from an allowed DeerFlow file capability endpoint`

The ingest URL does not match the allowed DeerFlow capability bases.

Set:

```dotenv
RAGFLOW_MCP_FILE_URI_ALLOWED_BASE_URLS=https://gateway.example
```

### Docker cannot connect to the daemon

Start Docker Desktop or Docker Engine, then run:

```bash
docker build -t ragflow-mcp-server:latest .
```
