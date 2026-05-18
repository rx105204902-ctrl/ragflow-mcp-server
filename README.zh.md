# RAGFlow MCP Server

[英文](README.md) | 中文

RAGFlow MCP Server 将 RAGFlow 的部分能力封装为 Model Context Protocol 服务。支持 MCP 的客户端可以通过它列出数据集、导入文档，并从指定的 RAGFlow 后端执行知识检索。

## 核心功能

- 将 RAGFlow 的数据集列表、文档导入和检索能力暴露为 MCP 工具。
- 同时支持 Streamable HTTP (`/mcp`) 和 legacy SSE (`/sse`) 两种传输方式。
- 支持 `self-host` 单密钥模式和 `host` 多租户请求密钥模式。
- 文档导入仅允许受信任的 DeerFlow file capability URL。
- 支持本地启动、Docker 启动和 Docker Compose 启动。
- API key 只在运行时注入，不写入镜像或源码文件。

## 目录

- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [前置条件](#前置条件)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [MCP 端点](#mcp-端点)
- [MCP 工具](#mcp-工具)
- [客户端示例](#客户端示例)
- [Docker](#docker)
- [测试](#测试)
- [架构说明](#架构说明)
- [安全说明](#安全说明)
- [故障排查](#故障排查)

## 技术栈

| 领域 | 技术 |
| --- | --- |
| 编程语言 | Python 3.12+ |
| MCP 框架 | FastMCP 3.x |
| MCP SDK | `mcp>=1.24.0` |
| HTTP 客户端 | HTTPX |
| ASGI 服务 | Uvicorn |
| ASGI 中间件 | Starlette |
| 命令行 | Click |
| 数据校验 | Pydantic |
| 环境变量加载 | python-dotenv |
| 容器运行 | Docker / Docker Compose |

## 项目结构

```text
.
|-- server/
|   `-- server.py                  # FastMCP 服务、RAGFlow 连接器、ASGI 应用、命令行入口
|-- client/
|   |-- client.py                  # SSE 客户端示例
|   `-- streamable_http_client.py  # Streamable HTTP 客户端示例
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

## 前置条件

本地开发需要：

- Python 3.12 或更高版本。
- 使用 `uv` 执行一次性启动命令，或使用 `pip` 和虚拟环境。
- 一个可以访问的 RAGFlow 后端。
- 使用 `self-host` 模式时需要 RAGFlow API key。

Docker 启动需要：

- Docker Engine 或 Docker Desktop。
- 使用 `docker compose` 时需要 Docker Compose v2。

## 快速开始

### 使用 uv 启动

第 1 步：如果本机还没有安装 `uv`，先安装 `uv`。

```bash
python -m pip install uv
```

第 2 步：创建本地环境并安装依赖。

```bash
uv venv .venv
uv pip install -r requirements.txt
```

第 3 步：启动 MCP 服务。

```bash
uv run python server/server.py \
  --host=0.0.0.0 \
  --port=9388 \
  --base-url=http://127.0.0.1:9380 \
  --mode=self-host \
  --api-key=ragflow-your-api-key
```

启动后可访问：

- Streamable HTTP：`http://localhost:9388/mcp`
- SSE：`http://localhost:9388/sse`

### 使用虚拟环境启动

PowerShell：

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

Bash：

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

## 配置说明

服务同时支持命令行参数和环境变量。程序会加载 `.env`，并让环境变量覆盖命令行传入值。

| 命令行参数 | 环境变量 | 代码默认值 | Docker 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `--host` | `RAGFLOW_MCP_HOST` | `127.0.0.1` | `0.0.0.0` | MCP 服务监听地址。 |
| `--port` | `RAGFLOW_MCP_PORT` | `9382` | `9388` | MCP 服务监听端口。 |
| `--base-url` | `RAGFLOW_MCP_BASE_URL` | `http://127.0.0.1:9380` | `http://127.0.0.1:9380` | RAGFlow 后端地址。 |
| `--mode` | `RAGFLOW_MCP_LAUNCH_MODE` | `self-host` | `self-host` | 启动模式，可选 `self-host` 或 `host`。 |
| `--api-key` | `RAGFLOW_MCP_HOST_API_KEY` | 空 | 必填 | `self-host` 模式使用的 RAGFlow API key。 |
| `--transport-sse-enabled` / `--no-transport-sse-enabled` | `RAGFLOW_MCP_TRANSPORT_SSE_ENABLED` | `true` | `true` | 是否启用 SSE 传输。 |
| `--transport-streamable-http-enabled` / `--no-transport-streamable-http-enabled` | `RAGFLOW_MCP_TRANSPORT_STREAMABLE_ENABLED` | `true` | `true` | 是否启用 Streamable HTTP 传输。 |
| `--json-response` / `--no-json-response` | `RAGFLOW_MCP_JSON_RESPONSE` | `true` | `true` | Streamable HTTP 是否使用 JSON 响应。 |
| 无 | `RAGFLOW_MCP_FILE_URI_ALLOWED_BASE_URLS` | 空 | 空 | 受信任 DeerFlow file capability 基础地址，多个值用英文逗号分隔。 |

### 启动模式

`self-host` 模式：

- 服务启动时提供一个 RAGFlow API key。
- 所有 MCP 请求都会使用该服务端密钥访问 RAGFlow。
- 必须提供 `--api-key` 或 `RAGFLOW_MCP_HOST_API_KEY`。

`host` 模式：

- 服务作为多租户网关运行。
- 每个 HTTP 请求都必须携带 `Authorization: Bearer ...`、`api_key` 或 `x-api-key`。
- 请求中的密钥会被转发给 RAGFlow。

### `.env` 示例

不要提交包含真实密钥的 `.env` 文件。

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

## MCP 端点

| 端点 | 传输方式 | 默认状态 | 用途 |
| --- | --- | --- | --- |
| `/mcp` | Streamable HTTP | 启用 | 现代 MCP 客户端的主要端点。 |
| `/sse` | SSE | 启用 | 旧版 SSE 传输端点。 |

当两种传输都启用时，服务会将两组路由合并到同一个 Starlette 应用中。

## MCP 工具

### `list_datasets`

列出当前 RAGFlow API key 可访问的数据集。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page` | integer | 否 | `1` | 数据集页码。 |
| `page_size` | integer | 否 | `30` | 返回数量，最大值为 `1000`。 |
| `id` | string | 否 | `null` | 可选的数据集 ID 过滤条件。 |
| `name` | string | 否 | `null` | 可选的数据集名称过滤条件。 |

示例：

```json
{
  "page": 1,
  "page_size": 30
}
```

### `document_ingest`

从受信任的 DeerFlow file capability URL 下载文件，将文件上传到 RAGFlow 数据集，并提交解析任务。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `dataset_id` | string | 是 | 目标 RAGFlow 数据集 ID。 |
| `files` | array | 是 | 文件描述列表。每一项必须包含 `file_uri`，`filename` 可选。 |

示例：

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

`file_uri` 校验规则：

- `file_uri` 必须使用 HTTP 或 HTTPS。
- 不允许携带用户名、密码、查询字符串和片段。
- 不允许路径穿越、本地文件路径和任意外部 URL。
- 如果 `RAGFLOW_MCP_FILE_URI_ALLOWED_BASE_URLS` 为空，默认允许：
  - `http://localhost:8001/api/file-capabilities`
  - `http://127.0.0.1:8001/api/file-capabilities`
- 如需允许自定义 DeerFlow 网关，请设置 `RAGFLOW_MCP_FILE_URI_ALLOWED_BASE_URLS`。

示例：

```dotenv
RAGFLOW_MCP_FILE_URI_ALLOWED_BASE_URLS=https://gateway.example,https://files.example/api
```

### `retrieval`

根据问题从 RAGFlow 检索相关内容片段。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `question` | string | 是 | 无 | 查询文本。 |
| `dataset_ids` | array | 否 | `[]` | 数据集过滤条件。为空时服务会解析当前密钥可访问的数据集。 |
| `document_ids` | array | 否 | `[]` | 文档过滤条件。 |
| `page` | integer | 否 | `1` | 结果页码。 |
| `page_size` | integer | 否 | `10` | 每页结果数量，最大值为 `100`。 |
| `similarity_threshold` | float | 否 | `0.2` | 最低相似度阈值。 |
| `vector_similarity_weight` | float | 否 | `0.3` | 向量相似度权重。 |
| `keyword` | boolean | 否 | `false` | 是否启用关键词检索。 |
| `top_k` | integer | 否 | `1024` | 排序前最多候选数量。 |
| `rerank_id` | string | 否 | `null` | 可选的重排模型 ID。 |
| `force_refresh` | boolean | 否 | `false` | 是否强制刷新元数据缓存。 |

示例：

```json
{
  "dataset_ids": ["dataset-1"],
  "document_ids": [],
  "question": "How to install neovim?",
  "page": 1,
  "page_size": 10
}
```

## 客户端示例

`client/` 目录包含两个最小示例。

运行 Streamable HTTP 客户端：

```bash
python client/streamable_http_client.py
```

运行 SSE 客户端：

```bash
python client/client.py
```

`host` 模式下，客户端需要携带以下任一请求头：

```python
headers = {"Authorization": "Bearer ragflow-your-api-key"}
```

```python
headers = {"api_key": "ragflow-your-api-key"}
```

示例客户端当前使用端口 `9382`。如果使用本仓库 Docker 默认配置启动服务，请改用端口 `9388`。

## Docker

更完整的 Docker 说明见 [DOCKER.md](DOCKER.md)。

构建镜像：

```bash
docker build -t ragflow-mcp-server:latest .
```

使用默认容器端口启动：

```bash
docker run --rm \
  --name ragflow-mcp-server \
  -p 9388:9388 \
  -e RAGFLOW_MCP_HOST_API_KEY=ragflow-your-api-key \
  ragflow-mcp-server:latest
```

如果 RAGFlow 后端运行在宿主机，Docker Desktop 用户应使用 `host.docker.internal`：

```bash
docker run --rm \
  --name ragflow-mcp-server \
  -p 9388:9388 \
  -e RAGFLOW_MCP_BASE_URL=http://host.docker.internal:9380 \
  -e RAGFLOW_MCP_HOST_API_KEY=ragflow-your-api-key \
  ragflow-mcp-server:latest
```

使用 Docker Compose：

```bash
export RAGFLOW_MCP_HOST_API_KEY=ragflow-your-api-key
export RAGFLOW_MCP_BASE_URL=http://host.docker.internal:9380
docker compose up --build
```

PowerShell：

```powershell
$env:RAGFLOW_MCP_HOST_API_KEY = "ragflow-your-api-key"
$env:RAGFLOW_MCP_BASE_URL = "http://host.docker.internal:9380"
docker compose up --build
```

停止 Compose 服务：

```bash
docker compose down
```

## 测试

安装运行依赖和测试依赖：

```bash
python -m pip install -r requirements.txt pytest pytest-asyncio
```

运行全部测试：

```bash
pytest
```

运行当前重点测试文件：

```bash
pytest tests/test_document_ingest_file_uri.py
```

测试会为 FastMCP 内部模块安装轻量桩对象，因此不需要启动真实 MCP 服务或 RAGFlow 后端，也可以测试文件 URL 校验和上传行为。

## 架构说明

### 启动流程

1. `server/server.py` 中的 Click 解析命令行参数。
2. `python-dotenv` 加载 `.env`。
3. 环境变量覆盖命令行参数。
4. FastMCP 注册工具和生命周期上下文。
5. `create_starlette_app()` 为 `/mcp`、`/sse` 或两者创建 ASGI 应用。
6. Uvicorn 启动 ASGI 应用。

### RAGFlow 连接器

`RAGFlowConnector` 负责和 RAGFlow 通信：

- 维护一个异步 HTTPX 客户端。
- 向 RAGFlow 转发 Bearer 凭据。
- 将后端失败转换为 MCP 工具错误。
- 为检索映射缓存数据集和文档元数据。
- 使用 multipart form data 上传文档字节。

### 鉴权流程

`self-host` 模式：

```text
MCP client -> MCP server -> RAGFlow
                  uses startup API key
```

`host` 模式：

```text
MCP client -> request auth header -> MCP server -> RAGFlow
```

### 文档导入流程

```text
document_ingest
  -> 校验 dataset_id 和 files
  -> 根据允许的 DeerFlow capability 基础地址校验 file_uri
  -> 从 DeerFlow 网关获取文件字节
  -> 推断或校验文件名
  -> 上传字节到 RAGFlow
  -> 提交解析任务
  -> 返回接收数量和提交数量
```

### 检索流程

```text
retrieval
  -> 校验查询和过滤条件
  -> dataset_ids 为空时解析可访问数据集
  -> 按需加载文档元数据
  -> 调用 RAGFlow 检索接口
  -> 返回 MCP 文本内容
```

## 安全说明

- 不要提交真实 RAGFlow API key。
- 使用 `.env`、Docker Compose 环境变量、Docker secrets 或部署平台的密钥管理能力。
- `document_ingest` 会主动拒绝任意 URL 和本地路径。
- Docker 镜像默认使用非 root 用户运行。
- Docker healthcheck 只检查 TCP 端口是否可连接。
- 如果服务需要暴露到本机之外，建议使用 `host` 模式、逐请求凭据，并在服务前配置 TLS。

## 故障排查

### `--api-key is required when --mode is 'self-host'`

服务以 `self-host` 模式启动，但没有提供 API key。

修复方式：

```bash
python server/server.py \
  --mode=self-host \
  --api-key=ragflow-your-api-key
```

或：

```bash
RAGFLOW_MCP_HOST_API_KEY=ragflow-your-api-key python server/server.py
```

### 容器无法访问 `127.0.0.1:9380`

在容器内部，`127.0.0.1` 指向容器自身，不是宿主机。

使用：

```bash
-e RAGFLOW_MCP_BASE_URL=http://host.docker.internal:9380
```

Linux 环境可增加：

```bash
--add-host=host.docker.internal:host-gateway
```

### `Missing or invalid authorization header`

服务很可能运行在 `host` 模式，但客户端没有发送凭据。

使用以下任一请求头：

```text
Authorization: Bearer ragflow-your-api-key
```

```text
api_key: ragflow-your-api-key
```

### `file_uri is not from an allowed DeerFlow file capability endpoint`

导入 URL 不匹配允许的 DeerFlow capability 基础地址。

设置：

```dotenv
RAGFLOW_MCP_FILE_URI_ALLOWED_BASE_URLS=https://gateway.example
```

### Docker 无法连接 daemon

启动 Docker Desktop 或 Docker Engine，然后重新执行：

```bash
docker build -t ragflow-mcp-server:latest .
```
