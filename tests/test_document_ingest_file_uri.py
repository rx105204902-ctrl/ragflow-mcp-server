import enum
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError


def _install_fastmcp_stubs(monkeypatch) -> None:
    fastmcp = ModuleType("fastmcp")

    class Context:
        def __init__(self):
            self.lifespan_context = {}

    class FastMCP:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def tool(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

        async def list_tools(self, *args, **kwargs):
            return []

        def http_app(self, *args, **kwargs):
            return SimpleNamespace(routes=[], lifespan=None, state=SimpleNamespace())

    fastmcp.Context = Context
    fastmcp.FastMCP = FastMCP

    dependencies = ModuleType("fastmcp.dependencies")
    dependencies.CurrentContext = lambda: Context()
    dependencies.CurrentHeaders = lambda: {}

    exceptions = ModuleType("fastmcp.exceptions")

    class ToolError(Exception):
        pass

    exceptions.ToolError = ToolError

    server_http = ModuleType("fastmcp.server.http")
    server_http.create_base_app = lambda **kwargs: SimpleNamespace(routes=kwargs.get("routes", []), lifespan=kwargs.get("lifespan"), state=SimpleNamespace())

    json_schema = ModuleType("fastmcp.utilities.json_schema")
    json_schema.dereference_refs = lambda schema: schema

    lifespan = ModuleType("fastmcp.utilities.lifespan")
    lifespan.combine_lifespans = lambda *args, **kwargs: None

    strenum = ModuleType("strenum")
    strenum.StrEnum = enum.StrEnum

    mcp_pkg = ModuleType("mcp")
    mcp_types = ModuleType("mcp.types")

    @dataclass
    class TextContent:
        type: str
        text: str

    class ImageContent:
        pass

    class EmbeddedResource:
        pass

    class Tool:
        pass

    mcp_types.TextContent = TextContent
    mcp_types.ImageContent = ImageContent
    mcp_types.EmbeddedResource = EmbeddedResource
    mcp_types.Tool = Tool
    mcp_pkg.types = mcp_types

    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp)
    monkeypatch.setitem(sys.modules, "fastmcp.dependencies", dependencies)
    monkeypatch.setitem(sys.modules, "fastmcp.exceptions", exceptions)
    monkeypatch.setitem(sys.modules, "fastmcp.server.http", server_http)
    monkeypatch.setitem(sys.modules, "fastmcp.utilities.json_schema", json_schema)
    monkeypatch.setitem(sys.modules, "fastmcp.utilities.lifespan", lifespan)
    monkeypatch.setitem(sys.modules, "strenum", strenum)
    monkeypatch.setitem(sys.modules, "mcp", mcp_pkg)
    monkeypatch.setitem(sys.modules, "mcp.types", mcp_types)


@pytest.fixture()
def ragflow_server(monkeypatch):
    _install_fastmcp_stubs(monkeypatch)
    module_name = "ragflow_server_under_test"
    sys.modules.pop(module_name, None)
    module_path = Path(__file__).resolve().parents[1] / "server" / "server.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_document_ingest_file_model_requires_file_uri(ragflow_server):
    uri_file = ragflow_server.DocumentIngestFile(file_uri="https://gateway.example/api/file-capabilities/token")

    assert uri_file.filename is None
    assert uri_file.file_uri.endswith("/token")

    with pytest.raises(ValidationError):
        ragflow_server.DocumentIngestFile(filename="demo.txt", content=b"hello")

    with pytest.raises(ValidationError):
        ragflow_server.DocumentIngestFile(filename="demo.txt")

    with pytest.raises(ValidationError):
        ragflow_server.DocumentIngestFile(file_uri="")


@pytest.mark.asyncio
async def test_prepare_ingest_files_accepts_allowed_file_uri_and_uses_response_filename(ragflow_server, monkeypatch):
    ragflow_server.FILE_URI_ALLOWED_BASE_URLS = "https://gateway.example"
    connector = ragflow_server.RAGFlowConnector("http://ragflow.example")

    async def fake_fetch(file_uri, file_index):
        assert file_uri == "https://gateway.example/api/file-capabilities/token"
        assert file_index == 0
        return b"pdf-bytes", "from-header.pdf"

    monkeypatch.setattr(connector, "_fetch_file_uri_content", fake_fetch)

    dataset_id, binary_files, empty_documents = await connector._prepare_ingest_files(
        "dataset-1",
        [{"file_uri": "https://gateway.example/api/file-capabilities/token"}],
    )

    assert dataset_id == "dataset-1"
    assert binary_files == [("from-header.pdf", b"pdf-bytes")]
    assert empty_documents == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_uri",
    [
        "http://localhost:8001/api/file-capabilities/token",
        "http://127.0.0.1:8001/api/file-capabilities/token",
    ],
)
async def test_prepare_ingest_files_accepts_default_deerflow_file_uri_bases(ragflow_server, monkeypatch, file_uri):
    ragflow_server.FILE_URI_ALLOWED_BASE_URLS = ""
    connector = ragflow_server.RAGFlowConnector("http://ragflow.example")

    async def fake_fetch(actual_file_uri, file_index):
        assert actual_file_uri == file_uri
        assert file_index == 0
        return b"markdown-bytes", "autumn.md"

    monkeypatch.setattr(connector, "_fetch_file_uri_content", fake_fetch)

    dataset_id, binary_files, empty_documents = await connector._prepare_ingest_files(
        "dataset-1",
        [{"file_uri": file_uri}],
    )

    assert dataset_id == "dataset-1"
    assert binary_files == [("autumn.md", b"markdown-bytes")]
    assert empty_documents == []


@pytest.mark.asyncio
async def test_prepare_ingest_files_default_file_uri_bases_still_reject_external_hosts(ragflow_server):
    ragflow_server.FILE_URI_ALLOWED_BASE_URLS = ""
    connector = ragflow_server.RAGFlowConnector("http://ragflow.example")

    with pytest.raises(ValueError, match="not from an allowed DeerFlow file capability endpoint"):
        await connector._prepare_ingest_files(
            "dataset-1",
            [{"file_uri": "http://example.com/api/file-capabilities/token", "filename": "demo.md"}],
        )


@pytest.mark.asyncio
async def test_prepare_ingest_files_rejects_inline_content(ragflow_server):
    connector = ragflow_server.RAGFlowConnector("http://ragflow.example")

    with pytest.raises(ValueError, match="unsupported field\\(s\\): content"):
        await connector._prepare_ingest_files(
            "dataset-1",
            [{"filename": "demo.txt", "content": "hello"}],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_uri",
    [
        "https://example.com/api/file-capabilities/token",
        "http://example.com/a.pdf",
        "https://gateway.example/api/file-capabilities/",
        "https://gateway.example/api/file-capabilities//token",
        "https://gateway.example/api/file-capabilities/../health",
        "https://gateway.example/api/file-capabilities/%2e%2e/health",
        "https://gateway.example/api/file-capabilities/token/extra",
        "https://gateway.example/api/file-capabilities/token%2fextra",
        "https://gateway.example/api/file-capabilities/token%5cextra",
        "/mnt/user-data/uploads/a.pdf",
        "/mnt/user-data/workspace/a.pdf",
        "/mnt/user-data/outputs/a.pdf",
        "../uploads/a.pdf",
        "/etc/passwd",
        r"C:\Windows\win.ini",
    ],
)
async def test_prepare_ingest_files_rejects_untrusted_file_uri_sources(ragflow_server, file_uri):
    ragflow_server.FILE_URI_ALLOWED_BASE_URLS = "https://gateway.example"
    connector = ragflow_server.RAGFlowConnector("http://ragflow.example")

    with pytest.raises(ValueError):
        await connector._prepare_ingest_files("dataset-1", [{"file_uri": file_uri, "filename": "demo.pdf"}])


@pytest.mark.asyncio
async def test_document_ingest_forwards_fetched_file_uri_bytes_to_ragflow_upload(ragflow_server, monkeypatch):
    ragflow_server.FILE_URI_ALLOWED_BASE_URLS = "https://gateway.example"
    connector = ragflow_server.RAGFlowConnector("http://ragflow.example")
    captured = {}

    async def fake_fetch(file_uri, file_index):
        return b"document-bytes", "demo.pdf"

    async def fake_upload(*, api_key, dataset_id, binary_files):
        captured["api_key"] = api_key
        captured["dataset_id"] = dataset_id
        captured["binary_files"] = binary_files
        return [{"id": "doc-1", "name": "demo.pdf"}]

    def fake_schedule_parse(*, api_key, dataset_id, document_ids):
        captured["parse"] = (api_key, dataset_id, document_ids)
        return True

    monkeypatch.setattr(connector, "_fetch_file_uri_content", fake_fetch)
    monkeypatch.setattr(connector, "_upload_binary_documents", fake_upload)
    monkeypatch.setattr(connector, "_schedule_parse", fake_schedule_parse)

    result = await connector.document_ingest(
        api_key="ragflow-key",
        dataset_id="dataset-1",
        files=[{"file_uri": "https://gateway.example/api/file-capabilities/token"}],
    )

    response = json.loads(result[0].text)
    assert captured["api_key"] == "ragflow-key"
    assert captured["dataset_id"] == "dataset-1"
    assert captured["binary_files"] == [("demo.pdf", b"document-bytes")]
    assert captured["parse"] == ("ragflow-key", "dataset-1", ["doc-1"])
    assert response["accepted"] is True
    assert response["submitted_file_count"] == 1
    assert response["accepted_document_count"] == 1
    assert response["parse_submission"] == "scheduled"


@pytest.mark.asyncio
async def test_call_tool_accepts_unprefixed_document_ingest_name(ragflow_server, monkeypatch):
    connector = ragflow_server.RAGFlowConnector("http://ragflow.example")
    captured = {}

    async def fake_document_ingest(*, api_key, dataset_id, files):
        captured["api_key"] = api_key
        captured["dataset_id"] = dataset_id
        captured["files"] = files
        return [ragflow_server.types.TextContent(type="text", text='{"accepted": true}')]

    monkeypatch.setattr(connector, "document_ingest", fake_document_ingest)

    result = await ragflow_server.call_tool(
        "document_ingest",
        {
            "dataset_id": "dataset-1",
            "files": [{"file_uri": "https://gateway.example/api/file-capabilities/token"}],
        },
        connector=connector,
        api_key="ragflow-key",
    )

    assert result[0].text == '{"accepted": true}'
    assert captured == {
        "api_key": "ragflow-key",
        "dataset_id": "dataset-1",
        "files": [{"file_uri": "https://gateway.example/api/file-capabilities/token"}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    ["ragflow_list_datasets", "ragflow_document_ingest", "ragflow_retrieval"],
)
async def test_call_tool_rejects_prefixed_aliases(ragflow_server, tool_name):
    connector = ragflow_server.RAGFlowConnector("http://ragflow.example")

    with pytest.raises(ValueError, match=f"Tool not found: {tool_name}"):
        await ragflow_server.call_tool(tool_name, {}, connector=connector, api_key="ragflow-key")


@pytest.mark.asyncio
async def test_upload_binary_documents_posts_multipart_file_bytes_to_ragflow(ragflow_server, monkeypatch):
    connector = ragflow_server.RAGFlowConnector("http://ragflow.example")
    captured = {}

    async def fake_post(path, *, files=None, api_key="", json=None, stream=False):
        captured["path"] = path
        captured["api_key"] = api_key
        captured["files"] = files
        captured["json"] = json
        captured["stream"] = stream
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [{"id": "doc-1", "name": "demo.pdf"}],
            },
        )

    monkeypatch.setattr(connector, "_post", fake_post)

    uploaded = await connector._upload_binary_documents(
        api_key="ragflow-key",
        dataset_id="dataset-1",
        binary_files=[("demo.pdf", b"%PDF-binary")],
    )

    assert uploaded == [{"id": "doc-1", "name": "demo.pdf"}]
    assert captured["path"] == "/datasets/dataset-1/documents"
    assert captured["api_key"] == "ragflow-key"
    assert captured["json"] is None
    assert captured["files"] == [
        ("file", ("demo.pdf", b"%PDF-binary", "application/octet-stream")),
    ]


@pytest.mark.asyncio
async def test_fetch_file_uri_content_returns_bytes_and_deerflow_filename_header(ragflow_server, monkeypatch):
    connector = ragflow_server.RAGFlowConnector("http://ragflow.example")

    class FakeClient:
        async def get(self, url):
            return httpx.Response(200, content=b"downloaded", headers={"X-DeerFlow-Filename": "header.pdf"})

    async def fake_get_client():
        return FakeClient()

    monkeypatch.setattr(connector, "_get_client", fake_get_client)

    content, filename = await connector._fetch_file_uri_content("https://gateway.example/api/file-capabilities/token", 0)

    assert content == b"downloaded"
    assert filename == "header.pdf"


@pytest.mark.asyncio
async def test_fetch_file_uri_content_decodes_percent_encoded_deerflow_filename_header(ragflow_server, monkeypatch):
    connector = ragflow_server.RAGFlowConnector("http://ragflow.example")

    class FakeClient:
        async def get(self, url):
            return httpx.Response(
                200,
                content="秋天".encode("utf-8"),
                headers={"X-DeerFlow-Filename": "%E7%A7%8B%E5%A4%A9.md"},
            )

    async def fake_get_client():
        return FakeClient()

    monkeypatch.setattr(connector, "_get_client", fake_get_client)

    content, filename = await connector._fetch_file_uri_content("https://gateway.example/api/file-capabilities/token", 0)

    assert content == "秋天".encode("utf-8")
    assert filename == "秋天.md"


@pytest.mark.asyncio
async def test_fetch_file_uri_content_maps_gateway_errors_to_clear_validation_errors(ragflow_server, monkeypatch):
    connector = ragflow_server.RAGFlowConnector("http://ragflow.example")

    class FakeClient:
        async def get(self, url):
            return httpx.Response(404, text="capability file missing")

    async def fake_get_client():
        return FakeClient()

    monkeypatch.setattr(connector, "_get_client", fake_get_client)

    with pytest.raises(ValueError, match="HTTP 404.*capability file missing"):
        await connector._fetch_file_uri_content("https://gateway.example/api/file-capabilities/token", 0)
