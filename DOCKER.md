# Docker 启动说明

本项目原始启动命令使用 `uv run --with ...` 临时注入依赖。Docker 镜像已经把同一组依赖写入 `requirements.txt` 并在构建阶段安装，因此容器运行时不会再下载 Python 依赖。

依赖来源等价于：

```bash
uv run \
  --with 'fastmcp>=3.0.0,<4.0.0' \
  --with 'mcp>=1.24.0' \
  --with click \
  --with httpx \
  --with pydantic \
  --with starlette \
  --with uvicorn \
  --with python-dotenv \
  --with strenum \
  python ragflow-mcp-server/server/server.py \
  --host=0.0.0.0 \
  --port=9388 \
  --base-url=http://127.0.0.1:9380 \
  --mode=self-host \
  --api-key=<runtime-required>
```

容器实际启动命令为：

```bash
python /app/server/server.py \
  --host="${RAGFLOW_MCP_HOST}" \
  --port="${RAGFLOW_MCP_PORT}" \
  --base-url="${RAGFLOW_MCP_BASE_URL}" \
  --mode="${RAGFLOW_MCP_LAUNCH_MODE}" \
  --api-key="${RAGFLOW_MCP_HOST_API_KEY}"
```

`--host`、`--port`、`--base-url`、`--mode` 都可以通过环境变量配置，并已经设置默认值。`--api-key` 不提供默认值，必须在启动容器时主动填写。

## 构建镜像

```bash
docker build -t ragflow-mcp-server:latest .
```

## 使用 docker run 启动

最小启动命令：

```bash
docker run --rm \
  --name ragflow-mcp-server \
  -p 9388:9388 \
  -e RAGFLOW_MCP_HOST_API_KEY=ragflow-your-api-key \
  ragflow-mcp-server:latest
```

完整可配置示例：

```bash
docker run --rm \
  --name ragflow-mcp-server \
  -p 9388:9388 \
  -e RAGFLOW_MCP_HOST=0.0.0.0 \
  -e RAGFLOW_MCP_PORT=9388 \
  -e RAGFLOW_MCP_BASE_URL=http://host.docker.internal:9380 \
  -e RAGFLOW_MCP_LAUNCH_MODE=self-host \
  -e RAGFLOW_MCP_HOST_API_KEY=ragflow-your-api-key \
  ragflow-mcp-server:latest
```

如果 RAGFlow 后端运行在宿主机，容器内的 `127.0.0.1` 指向容器自身，不是宿主机。Docker Desktop 通常可以使用 `http://host.docker.internal:9380` 访问宿主机服务；Linux Docker 环境可改用宿主机网关地址，或在启动时增加 `--add-host=host.docker.internal:host-gateway`。

服务启动后：

- Streamable HTTP 端点：`http://localhost:9388/mcp`
- SSE 端点：`http://localhost:9388/sse`

## 使用 Docker Compose 启动

PowerShell 示例：

```powershell
$env:RAGFLOW_MCP_HOST_API_KEY = "ragflow-your-api-key"
$env:RAGFLOW_MCP_BASE_URL = "http://host.docker.internal:9380"
docker compose up --build
```

Bash 示例：

```bash
export RAGFLOW_MCP_HOST_API_KEY=ragflow-your-api-key
export RAGFLOW_MCP_BASE_URL=http://host.docker.internal:9380
docker compose up --build
```

后台启动：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f ragflow-mcp-server
```

停止服务：

```bash
docker compose down
```

## 配置项

| 环境变量 | 对应参数 | 默认值 | 是否必填 | 说明 |
| --- | --- | --- | --- | --- |
| `RAGFLOW_MCP_HOST` | `--host` | `0.0.0.0` | 否 | 容器内监听地址。 |
| `RAGFLOW_MCP_PORT` | `--port` | `9388` | 否 | 容器内监听端口。 |
| `RAGFLOW_MCP_BASE_URL` | `--base-url` | `http://127.0.0.1:9380` | 否 | RAGFlow 后端地址。容器访问宿主机时建议改成 `http://host.docker.internal:9380`。 |
| `RAGFLOW_MCP_LAUNCH_MODE` | `--mode` | `self-host` | 否 | 可选值为 `self-host` 或 `host`。 |
| `RAGFLOW_MCP_HOST_API_KEY` | `--api-key` | 无 | 是 | RAGFlow API key。镜像不会内置该值。 |
| `RAGFLOW_MCP_TRANSPORT_SSE_ENABLED` | `--transport-sse-enabled` | `true` | 否 | 是否启用 SSE 传输。 |
| `RAGFLOW_MCP_TRANSPORT_STREAMABLE_ENABLED` | `--transport-streamable-http-enabled` | `true` | 否 | 是否启用 Streamable HTTP 传输。 |
| `RAGFLOW_MCP_JSON_RESPONSE` | `--json-response` | `true` | 否 | Streamable HTTP 是否使用 JSON 响应模式。 |
| `RAGFLOW_MCP_PUBLISHED_PORT` | Compose 端口映射 | `9388` | 否 | 仅 `docker-compose.yml` 使用，控制宿主机暴露端口。 |

## 生产运行建议

不要把真实 API key 写入 `Dockerfile`、镜像层或提交到仓库的配置文件。建议通过环境变量、Docker Compose 的本地 `.env` 文件、Docker secret 或部署平台的密钥管理能力注入。

容器默认使用非 root 用户运行，镜像构建上下文通过 `.dockerignore` 排除了测试缓存、客户端示例和本地运行状态文件。
