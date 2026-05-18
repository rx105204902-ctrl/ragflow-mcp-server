#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import asyncio
import json
import logging
import os
import random
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from urllib.parse import unquote, urlsplit, urlunsplit

import click
import httpx
import mcp.types as types
from fastmcp import Context, FastMCP
from fastmcp.dependencies import CurrentContext, CurrentHeaders
from fastmcp.exceptions import ToolError
from fastmcp.server.http import create_base_app
from fastmcp.utilities.json_schema import dereference_refs
from fastmcp.utilities.lifespan import combine_lifespans
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from strenum import StrEnum


class LaunchMode(StrEnum):
    SELF_HOST = "self-host"
    HOST = "host"


class Transport(StrEnum):
    SSE = "sse"
    STREAMABLE_HTTP = "streamable-http"


BASE_URL = "http://127.0.0.1:9380"
HOST = "127.0.0.1"
PORT = "9382"
HOST_API_KEY = ""
MODE = ""
TRANSPORT_SSE_ENABLED = True
TRANSPORT_STREAMABLE_HTTP_ENABLED = True
JSON_RESPONSE = True
FILE_URI_ALLOWED_BASE_URLS = ""
DEFAULT_FILE_URI_ALLOWED_BASE_URLS = ("http://localhost:8001", "http://127.0.0.1:8001")


class DocumentIngestFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str | None = Field(default=None, description="Optional document filename. Used when the file_uri source does not return X-DeerFlow-Filename.")
    file_uri: str = Field(description="DeerFlow file capability URL to fetch document bytes from.")

    @model_validator(mode="after")
    def validate_file_uri(self) -> "DocumentIngestFile":
        if not isinstance(self.file_uri, str) or not self.file_uri.strip():
            raise ValueError("file_uri must be a non-empty string.")
        return self


class RAGFlowConnector:
    _MAX_DATASET_CACHE = 32
    _CACHE_TTL = 300
    _DATASET_PAGE_SIZE = 1000

    _dataset_metadata_cache: OrderedDict[str, tuple[dict, float | int]] = OrderedDict()  # "dataset_id" -> (metadata, expiry_ts)
    _document_metadata_cache: OrderedDict[str, tuple[list[tuple[str, dict]], float | int]] = OrderedDict()  # "dataset_id" -> ([(document_id, doc_metadata)], expiry_ts)

    def __init__(self, base_url: str, version="v1"):
        self.base_url = base_url
        self.version = version
        self.api_url = f"{self.base_url}/api/{self.version}"
        self._async_client = None

    async def _get_client(self):
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        return self._async_client

    async def close(self):
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    async def _post(self, path, json=None, stream=False, files=None, api_key: str = ""):
        if not api_key:
            return None
        client = await self._get_client()
        kwargs = {"headers": {"Authorization": f"Bearer {api_key}"}}
        if json is not None:
            kwargs["json"] = json
        if files is not None:
            kwargs["files"] = files
        res = await client.post(url=self.api_url + path, **kwargs)
        return res

    async def _get(self, path, params=None, api_key: str = ""):
        if not api_key:
            return None
        client = await self._get_client()
        res = await client.get(url=self.api_url + path, params=params, headers={"Authorization": f"Bearer {api_key}"})
        return res

    def _is_cache_valid(self, ts):
        return time.time() < ts

    def _get_expiry_timestamp(self):
        offset = random.randint(-30, 30)
        return time.time() + self._CACHE_TTL + offset

    @staticmethod
    def _mcp_error(message: str):
        raise Exception([types.TextContent(type="text", text=message or "Cannot process this operation.")])

    @staticmethod
    def _compact_response_text(res, *, limit: int = 300) -> str:
        text = getattr(res, "text", "") or ""
        compact = " ".join(str(text).split())
        if len(compact) > limit:
            return compact[:limit] + "..."
        return compact

    @staticmethod
    def _check_allowed_fields(value: dict, allowed_fields: set[str], location: str):
        unexpected = sorted(set(value) - allowed_fields)
        if unexpected:
            fields = ", ".join(unexpected)
            allowed = ", ".join(sorted(allowed_fields))
            raise ValueError(f"{location} contains unsupported field(s): {fields}. Allowed fields: {allowed}.")

    @staticmethod
    def _validate_int_argument(value: Any, name: str, *, minimum: int, maximum: int | None = None) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer.")
        if value < minimum:
            raise ValueError(f"{name} must be greater than or equal to {minimum}.")
        if maximum is not None and value > maximum:
            raise ValueError(f"{name} must be less than or equal to {maximum}.")
        return value

    @staticmethod
    def _validate_optional_string(value: Any, name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string.")
        return value

    @staticmethod
    def _validate_optional_string_list(value: Any, name: str) -> list[str] | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError(f"{name} must be an array of strings.")

        result = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ValueError(f"{name}[{index}] must be a string.")
            item = item.strip()
            if item:
                result.append(item)
        return result

    @staticmethod
    def _validate_bool_argument(value: Any, name: str) -> bool:
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean.")
        return value

    @staticmethod
    def _validate_orderby(value: Any, name: str, allowed_fields: set[str]) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string.")
        if value not in allowed_fields:
            allowed = ", ".join(sorted(allowed_fields))
            raise ValueError(f"{name} must be one of: {allowed}.")
        return value

    @staticmethod
    def _exception_text(exc: Exception) -> str:
        if exc.args and isinstance(exc.args[0], list):
            parts = []
            for item in exc.args[0]:
                text = getattr(item, "text", None)
                if text:
                    parts.append(text)
            if parts:
                return "; ".join(parts)
        return str(exc)

    def _response_json(self, res, fallback_message: str):
        if res is None:
            self._mcp_error(fallback_message)
        if res.status_code != 200:
            message = fallback_message
            try:
                body = res.json()
                if isinstance(body, dict):
                    message = body.get("message") or body.get("error") or message
            except Exception:
                response_text = self._compact_response_text(res)
                if response_text:
                    message = f"{fallback_message} Backend HTTP {res.status_code}: {response_text}"
                else:
                    message = f"{fallback_message} Backend HTTP {res.status_code}."
            self._mcp_error(message)

        try:
            res_json = res.json()
        except Exception:
            self._mcp_error("Unexpected non-JSON response from RAGFlow backend.")

        if not isinstance(res_json, dict):
            self._mcp_error("Unexpected response format from RAGFlow backend.")

        if res_json.get("code") != 0:
            self._mcp_error(res_json.get("message", fallback_message))
        return res_json

    def _get_cached_dataset_metadata(self, dataset_id):
        entry = self._dataset_metadata_cache.get(dataset_id)
        if entry:
            data, ts = entry
            if self._is_cache_valid(ts):
                self._dataset_metadata_cache.move_to_end(dataset_id)
                return data
        return None

    def _set_cached_dataset_metadata(self, dataset_id, metadata):
        self._dataset_metadata_cache[dataset_id] = (metadata, self._get_expiry_timestamp())
        self._dataset_metadata_cache.move_to_end(dataset_id)
        if len(self._dataset_metadata_cache) > self._MAX_DATASET_CACHE:
            self._dataset_metadata_cache.popitem(last=False)

    def _get_cached_document_metadata_by_dataset(self, dataset_id):
        entry = self._document_metadata_cache.get(dataset_id)
        if entry:
            data_list, ts = entry
            if self._is_cache_valid(ts):
                self._document_metadata_cache.move_to_end(dataset_id)
                return {doc_id: doc_meta for doc_id, doc_meta in data_list}
        return None

    def _set_cached_document_metadata_by_dataset(self, dataset_id, doc_id_meta_list):
        self._document_metadata_cache[dataset_id] = (doc_id_meta_list, self._get_expiry_timestamp())
        self._document_metadata_cache.move_to_end(dataset_id)

    async def _fetch_datasets_page(
        self,
        *,
        api_key: str,
        page: int,
        page_size: int,
        orderby: str = "create_time",
        desc: bool = True,
        id: str | None = None,
        name: str | None = None,
    ):
        """Fetch one structured page of accessible datasets from the backend API."""
        params = {"page": page, "page_size": page_size, "orderby": orderby, "desc": desc}
        if id:
            params["id"] = id
        if name:
            params["name"] = name

        res = await self._get("/datasets", params, api_key=api_key)
        return self._response_json(res, "Cannot list datasets.")

    async def list_datasets(self, *, api_key: str, page: int = 1, page_size: int = 1000, orderby: str = "create_time", desc: bool = True, id: str | None = None, name: str | None = None):
        """Return accessible datasets as newline-delimited JSON for MCP tool descriptions."""
        res_json = await self._fetch_datasets_page(api_key=api_key, page=page, page_size=page_size, orderby=orderby, desc=desc, id=id, name=name)
        result_list = []
        data_list = res_json.get("data", [])
        if not isinstance(data_list, list):
            self._mcp_error("Unexpected dataset list response format.")
        for data in data_list:
            d = {"description": data.get("description", ""), "id": data["id"], "name": data.get("name", "")}
            result_list.append(json.dumps(d, ensure_ascii=False))
        return "\n".join(result_list)

    async def query_datasets(
        self,
        *,
        api_key: str,
        page: int = 1,
        page_size: int = 30,
        orderby: str = "create_time",
        desc: bool = True,
        id: str | None = None,
        name: str | None = None,
    ):
        """Return accessible datasets as structured MCP tool content."""
        res_json = await self._fetch_datasets_page(api_key=api_key, page=page, page_size=page_size, orderby=orderby, desc=desc, id=id, name=name)
        datasets = []
        data_list = res_json.get("data", [])
        if not isinstance(data_list, list):
            self._mcp_error("Unexpected dataset list response format.")
        for data in data_list:
            datasets.append(
                {
                    "id": data.get("id"),
                    "name": data.get("name", ""),
                    "description": data.get("description", ""),
                    "document_count": data.get("document_count"),
                    "chunk_count": data.get("chunk_count"),
                }
            )
        response = {
            "datasets": datasets,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": res_json.get("total", len(datasets)),
            },
        }
        return [types.TextContent(type="text", text=json.dumps(response, ensure_ascii=False))]

    @staticmethod
    def _document_summary(doc: dict) -> dict:
        return {
            "id": doc.get("id"),
            "name": doc.get("name", ""),
            "run": doc.get("run", ""),
            "progress": doc.get("progress", 0),
            "progress_msg": doc.get("progress_msg", ""),
            "chunk_count": doc.get("chunk_count", doc.get("chunk_num", 0)),
            "token_count": doc.get("token_count", doc.get("token_num", 0)),
            "size": doc.get("size"),
            "type": doc.get("type", ""),
            "source_type": doc.get("source_type", ""),
            "chunk_method": doc.get("chunk_method", ""),
            "status": doc.get("status", ""),
            "location": doc.get("location", ""),
            "create_time": doc.get("create_time"),
            "update_time": doc.get("update_time"),
            "create_date": doc.get("create_date", ""),
            "update_date": doc.get("update_date", ""),
            "metadata": doc.get("meta_fields", doc.get("metadata", {})),
        }

    async def query_documents(
        self,
        *,
        api_key: str,
        dataset_id: str,
        page: int = 1,
        page_size: int = 30,
        orderby: str = "create_time",
        desc: bool = True,
        id: str | None = None,
        ids: list[str] | None = None,
        name: str | None = None,
        keywords: str | None = None,
        suffix: list[str] | None = None,
        run: list[str] | None = None,
    ):
        """Return documents and parse status for one dataset as structured MCP tool content."""
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            self._mcp_error("dataset_id is required.")
        try:
            orderby = self._validate_orderby(orderby, "orderby", {"create_time", "update_time"})
            desc = self._validate_bool_argument(desc, "desc")
            id = self._validate_optional_string(id, "id")
            ids = self._validate_optional_string_list(ids, "ids")
            name = self._validate_optional_string(name, "name")
            keywords = self._validate_optional_string(keywords, "keywords")
            suffix = self._validate_optional_string_list(suffix, "suffix")
            run = self._validate_optional_string_list(run, "run")
        except ValueError as exc:
            self._mcp_error(str(exc))
        if id and ids:
            self._mcp_error("id and ids cannot be used together.")

        params: dict[str, Any] = {"page": page, "page_size": page_size, "orderby": orderby, "desc": desc}
        if id:
            params["id"] = id
        if ids:
            params["ids"] = ids
        if name:
            params["name"] = name
        if keywords:
            params["keywords"] = keywords
        if suffix:
            params["suffix"] = suffix
        if run:
            params["run"] = run

        dataset_id = dataset_id.strip()
        res = await self._get(f"/datasets/{dataset_id}/documents", params=params, api_key=api_key)
        res_json = self._response_json(res, "Cannot list documents.")
        data = res_json.get("data")
        if not isinstance(data, dict):
            self._mcp_error("Unexpected document list response format.")

        docs = data.get("docs", [])
        if not isinstance(docs, list) or not all(isinstance(doc, dict) for doc in docs):
            self._mcp_error("Unexpected document list response format.")

        response = {
            "dataset_id": dataset_id,
            "documents": [self._document_summary(doc) for doc in docs],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": data.get("total", len(docs)),
            },
        }
        return [types.TextContent(type="text", text=json.dumps(response, ensure_ascii=False))]

    @staticmethod
    def _safe_document_filename(filename: Any, file_index: int) -> str:
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError(f"files[{file_index}].filename is required.")
        candidate = filename.strip()
        if "/" in candidate or "\\" in candidate or candidate in {".", ".."}:
            raise ValueError(f"files[{file_index}].filename must be a plain filename.")
        if len(candidate.encode("utf-8")) > 255:
            raise ValueError(f"files[{file_index}].filename is too long.")
        return candidate

    @staticmethod
    def _file_uri_path_segments(path: str) -> tuple[str, ...]:
        decoded_path = unquote(path)
        if not decoded_path.startswith("/"):
            raise ValueError("file_uri path must be absolute.")
        segments = tuple(decoded_path.split("/")[1:])
        if not segments or any(segment in {"", ".", ".."} or "\\" in segment for segment in segments):
            raise ValueError("file_uri path contains unsafe segments.")
        return segments

    @classmethod
    def _allowed_file_uri_bases(cls) -> list[tuple[str, str, tuple[str, ...]]]:
        bases = []
        configured_values = [raw.strip() for raw in FILE_URI_ALLOWED_BASE_URLS.split(",") if raw.strip()]
        raw_values = configured_values or list(DEFAULT_FILE_URI_ALLOWED_BASE_URLS)
        for raw in raw_values:
            value = raw.strip().rstrip("/")
            if not value:
                continue
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                continue
            path = parsed.path.rstrip("/")
            if not path.endswith("/api/file-capabilities"):
                if path.endswith("/api"):
                    path = path + "/file-capabilities"
                else:
                    path = path + "/api/file-capabilities" if path else "/api/file-capabilities"
            try:
                base_segments = cls._file_uri_path_segments(path)
            except ValueError:
                continue
            bases.append((parsed.scheme.lower(), parsed.netloc.lower(), base_segments))
        return bases

    @classmethod
    def _validate_file_uri(cls, file_uri: Any, file_index: int) -> str:
        if not isinstance(file_uri, str) or not file_uri.strip():
            raise ValueError(f"files[{file_index}].file_uri must be a non-empty string.")
        value = file_uri.strip()
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError(f"files[{file_index}].file_uri must be a configured DeerFlow file capability URL.")
        if parsed.query or parsed.fragment:
            raise ValueError(f"files[{file_index}].file_uri must not include query strings or fragments.")
        normalized = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", ""))
        try:
            target_segments = cls._file_uri_path_segments(parsed.path)
        except ValueError as exc:
            raise ValueError(f"files[{file_index}].file_uri must be a configured DeerFlow file capability URL.") from exc
        bases = cls._allowed_file_uri_bases()
        if not bases:
            raise ValueError("file_uri support requires RAGFLOW_MCP_FILE_URI_ALLOWED_BASE_URLS.")
        for scheme, netloc, base_segments in bases:
            if parsed.scheme.lower() != scheme or parsed.netloc.lower() != netloc:
                continue
            if len(target_segments) == len(base_segments) + 1 and target_segments[: len(base_segments)] == base_segments:
                return normalized
        raise ValueError(f"files[{file_index}].file_uri is not from an allowed DeerFlow file capability endpoint.")

    @staticmethod
    def _filename_from_capability_response(res: httpx.Response) -> str | None:
        filename = res.headers.get("x-deerflow-filename")
        if isinstance(filename, str) and filename.strip():
            return unquote(filename.strip())
        return None

    async def _fetch_file_uri_content(self, file_uri: str, file_index: int) -> tuple[bytes, str | None]:
        client = await self._get_client()
        try:
            res = await client.get(file_uri)
        except Exception as exc:
            raise ValueError(f"files[{file_index}].file_uri could not be fetched by ragflow-mcp from DeerFlow Gateway: {self._exception_text(exc)}") from exc

        if res.status_code != 200:
            detail = self._compact_response_text(res)
            suffix = f": {detail}" if detail else ""
            raise ValueError(f"files[{file_index}].file_uri fetch from DeerFlow Gateway failed with HTTP {res.status_code}{suffix}")
        return bytes(res.content), self._filename_from_capability_response(res)

    async def _prepare_ingest_files(self, dataset_id: str, files: list[dict]):
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise ValueError("dataset_id is required.")
        if not isinstance(files, list) or not files:
            raise ValueError("files must be a non-empty array.")

        binary_files = []
        empty_documents = []
        allowed_file_fields = {"filename", "file_uri"}
        for index, item in enumerate(files):
            if not isinstance(item, dict):
                raise ValueError(f"files[{index}] must be an object.")
            self._check_allowed_fields(item, allowed_file_fields, f"files[{index}]")
            file_uri = self._validate_file_uri(item.get("file_uri"), index)
            content, response_filename = await self._fetch_file_uri_content(file_uri, index)
            filename = item.get("filename") or response_filename
            filename = self._safe_document_filename(filename, index)

            if content:
                binary_files.append((filename, content))
            else:
                empty_documents.append(filename)

        return dataset_id.strip(), binary_files, empty_documents

    async def _upload_binary_documents(self, *, api_key: str, dataset_id: str, binary_files: list[tuple[str, bytes]]):
        if not binary_files:
            return []
        multipart_files = [("file", (filename, content, "application/octet-stream")) for filename, content in binary_files]
        res = await self._post(f"/datasets/{dataset_id}/documents", files=multipart_files, api_key=api_key)
        res_json = self._response_json(res, "Cannot upload document files.")
        data = res_json.get("data", [])
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list) and all(isinstance(doc, dict) for doc in data):
            return data
        self._mcp_error("Unexpected upload response format.")

    async def _create_empty_document(self, *, api_key: str, dataset_id: str, filename: str):
        res = await self._post(f"/datasets/{dataset_id}/documents?type=empty", json={"name": filename}, api_key=api_key)
        res_json = self._response_json(res, f"Cannot create empty document {filename}.")
        data = res_json.get("data")
        if not isinstance(data, dict):
            self._mcp_error(f"Unexpected empty document response format for {filename}.")
        return data

    async def _trigger_parse(self, *, api_key: str, dataset_id: str, document_ids: list[str]):
        if not document_ids:
            return None
        res = await self._post(f"/datasets/{dataset_id}/documents/parse", json={"document_ids": document_ids}, api_key=api_key)
        return self._response_json(res, "Cannot trigger document parsing.")

    def _schedule_parse(self, *, api_key: str, dataset_id: str, document_ids: list[str]) -> bool:
        if not document_ids:
            return False

        async def _submit_parse():
            try:
                await self._trigger_parse(api_key=api_key, dataset_id=dataset_id, document_ids=document_ids)
            except Exception as exc:
                logging.warning(
                    "Failed to submit background document parse request: dataset_id=%s document_count=%s error=%s",
                    dataset_id,
                    len(document_ids),
                    self._exception_text(exc),
                )

        asyncio.create_task(_submit_parse())
        return True

    async def document_ingest(self, *, api_key: str, dataset_id: str, files: list[dict]):
        try:
            dataset_id, binary_files, empty_documents = await self._prepare_ingest_files(dataset_id, files)
        except ValueError as exc:
            self._mcp_error(str(exc))

        uploaded = []
        errors = []

        try:
            uploaded.extend(await self._upload_binary_documents(api_key=api_key, dataset_id=dataset_id, binary_files=binary_files))
        except Exception as exc:
            errors.append({"stage": "upload", "filenames": [filename for filename, _ in binary_files], "message": self._exception_text(exc)})

        for filename in empty_documents:
            try:
                uploaded.append(await self._create_empty_document(api_key=api_key, dataset_id=dataset_id, filename=filename))
            except Exception as exc:
                errors.append({"stage": "empty_document", "filename": filename, "message": self._exception_text(exc)})

        if not uploaded:
            if errors:
                self._mcp_error("; ".join(error["message"] for error in errors))
            self._mcp_error("No documents were uploaded.")

        uploaded_ids = [doc.get("id") for doc in uploaded if doc.get("id")]
        for doc in uploaded:
            if not doc.get("id"):
                errors.append(
                    {
                        "stage": "upload",
                        "filename": doc.get("name", ""),
                        "message": "Uploaded document response missing id; parse was not triggered for this document.",
                    }
                )

        parse_scheduled = self._schedule_parse(api_key=api_key, dataset_id=dataset_id, document_ids=uploaded_ids)

        response = {
            "dataset_id": dataset_id,
            "accepted": True,
            "message": "Document ingestion request was forwarded. Parsing runs asynchronously; query document status separately.",
            "submitted_file_count": len(binary_files) + len(empty_documents),
            "accepted_document_count": len(uploaded_ids),
            "parse_submission": "scheduled" if parse_scheduled else "not_scheduled",
        }
        if errors:
            response["errors"] = errors
        return [types.TextContent(type="text", text=json.dumps(response, ensure_ascii=False))]

    async def resolve_dataset_ids(self, *, api_key: str):
        """Resolve all accessible dataset IDs for MCP retrieval fallback."""
        logging.info("Resolving accessible dataset IDs for MCP retrieval")
        dataset_ids = []
        page = 1

        while True:
            logging.debug("resolve_dataset_ids fetching /datasets page=%s page_size=%s", page, self._DATASET_PAGE_SIZE)
            try:
                res_json = await self._fetch_datasets_page(api_key=api_key, page=page, page_size=self._DATASET_PAGE_SIZE)
            except Exception as exc:
                logging.warning("resolve_dataset_ids failed to fetch /datasets page=%s error=%s", page, exc)
                raise

            datasets = res_json.get("data", [])
            logging.debug("resolve_dataset_ids received %s datasets from page=%s", len(datasets), page)
            dataset_ids.extend(data["id"] for data in datasets if data.get("id"))
            total = res_json.get("total", len(dataset_ids))
            if not datasets or len(dataset_ids) >= total:
                break
            page += 1

        resolved = list(dict.fromkeys(dataset_ids))
        logging.info("resolve_dataset_ids resolved %s accessible dataset IDs", len(resolved))
        return resolved

    async def retrieval(
        self,
        *,
        api_key: str,
        dataset_ids,
        document_ids=None,
        question="",
        page=1,
        page_size=30,
        similarity_threshold=0.2,
        vector_similarity_weight=0.3,
        top_k=1024,
        rerank_id: str | None = None,
        keyword: bool = False,
        force_refresh: bool = False,
    ):
        if document_ids is None:
            document_ids = []

        if not dataset_ids:
            logging.info("MCP retrieval omitted dataset_ids; resolving accessible datasets")
            dataset_ids = await self.resolve_dataset_ids(api_key=api_key)
            if not dataset_ids:
                logging.info("MCP retrieval found no accessible datasets for current user")
                raise Exception([types.TextContent(type="text", text="No accessible datasets found.")])

        data_json = {
            "page": page,
            "page_size": page_size,
            "similarity_threshold": similarity_threshold,
            "vector_similarity_weight": vector_similarity_weight,
            "top_k": top_k,
            "rerank_id": rerank_id,
            "keyword": keyword,
            "question": question,
            "dataset_ids": dataset_ids,
            "document_ids": document_ids,
        }
        # Send a POST request to the backend service (using requests library as an example, actual implementation may vary)
        res = await self._post("/retrieval", json=data_json, api_key=api_key)
        if not res or res.status_code != 200:
            raise Exception([types.TextContent(type="text", text="Cannot process this operation.")])

        res = res.json()
        if res.get("code") == 0:
            data = res["data"]
            chunks = []

            # Cache document metadata and dataset information
            document_cache, dataset_cache = await self._get_document_metadata_cache(dataset_ids, api_key=api_key, force_refresh=force_refresh)

            # Process chunks with enhanced field mapping including per-chunk metadata
            for chunk_data in data.get("chunks", []):
                enhanced_chunk = self._map_chunk_fields(chunk_data, dataset_cache, document_cache)
                chunks.append(enhanced_chunk)

            # Build structured response (no longer need response-level document_metadata)
            response = {
                "chunks": chunks,
                "pagination": {
                    "page": data.get("page", page),
                    "page_size": data.get("page_size", page_size),
                    "total_chunks": data.get("total", len(chunks)),
                    "total_pages": (data.get("total", len(chunks)) + page_size - 1) // page_size,
                },
                "query_info": {
                    "question": question,
                    "similarity_threshold": similarity_threshold,
                    "vector_weight": vector_similarity_weight,
                    "keyword_search": keyword,
                    "dataset_count": len(dataset_ids),
                },
            }

            return [types.TextContent(type="text", text=json.dumps(response, ensure_ascii=False))]

        raise Exception([types.TextContent(type="text", text=res.get("message"))])

    async def _get_document_metadata_cache(self, dataset_ids, *, api_key: str, force_refresh=False):
        """Cache document metadata for all documents in the specified datasets"""
        document_cache = {}
        dataset_cache = {}

        try:
            for dataset_id in dataset_ids:
                dataset_meta = None if force_refresh else self._get_cached_dataset_metadata(dataset_id)
                if not dataset_meta:
                    # First get dataset info for name
                    dataset_res = await self._get("/datasets", {"id": dataset_id, "page_size": 1}, api_key=api_key)
                    if dataset_res and dataset_res.status_code == 200:
                        dataset_data = dataset_res.json()
                        if dataset_data.get("code") == 0 and dataset_data.get("data"):
                            dataset_info = dataset_data["data"][0]
                            dataset_meta = {"name": dataset_info.get("name", "Unknown"), "description": dataset_info.get("description", "")}
                            self._set_cached_dataset_metadata(dataset_id, dataset_meta)
                if dataset_meta:
                    dataset_cache[dataset_id] = dataset_meta

                docs = None if force_refresh else self._get_cached_document_metadata_by_dataset(dataset_id)
                if docs is None:
                    page = 1
                    page_size = 30
                    doc_id_meta_list = []
                    docs = {}
                    while page:
                        docs_res = await self._get(f"/datasets/{dataset_id}/documents?page={page}", api_key=api_key)
                        if not docs_res:
                            break
                        docs_data = docs_res.json()
                        if docs_data.get("code") == 0 and docs_data.get("data", {}).get("docs"):
                            for doc in docs_data["data"]["docs"]:
                                doc_id = doc.get("id")
                                if not doc_id:
                                    continue
                                doc_meta = {
                                    "document_id": doc_id,
                                    "name": doc.get("name", ""),
                                    "location": doc.get("location", ""),
                                    "type": doc.get("type", ""),
                                    "size": doc.get("size"),
                                    "chunk_count": doc.get("chunk_count"),
                                    "create_date": doc.get("create_date", ""),
                                    "update_date": doc.get("update_date", ""),
                                    "token_count": doc.get("token_count"),
                                    "thumbnail": doc.get("thumbnail", ""),
                                    "dataset_id": doc.get("dataset_id", dataset_id),
                                    "meta_fields": doc.get("meta_fields", {}),
                                }
                                doc_id_meta_list.append((doc_id, doc_meta))
                                docs[doc_id] = doc_meta

                            page += 1
                            if docs_data.get("data", {}).get("total", 0) - page * page_size <= 0:
                                page = None

                        self._set_cached_document_metadata_by_dataset(dataset_id, doc_id_meta_list)
                if docs:
                    document_cache.update(docs)

        except Exception as e:
            # Gracefully handle metadata cache failures
            logging.error(f"Problem building the document metadata cache: {str(e)}")
            pass

        return document_cache, dataset_cache

    def _map_chunk_fields(self, chunk_data, dataset_cache, document_cache):
        """Preserve all original API fields and add per-chunk document metadata"""
        # Start with ALL raw data from API (preserve everything like original version)
        mapped = dict(chunk_data)

        # Add dataset name enhancement
        dataset_id = chunk_data.get("dataset_id") or chunk_data.get("kb_id")
        if dataset_id and dataset_id in dataset_cache:
            mapped["dataset_name"] = dataset_cache[dataset_id]["name"]
        else:
            mapped["dataset_name"] = "Unknown"

        # Add document name convenience field
        mapped["document_name"] = chunk_data.get("document_keyword", "")

        # Add per-chunk document metadata
        document_id = chunk_data.get("document_id")
        if document_id and document_id in document_cache:
            mapped["document_metadata"] = document_cache[document_id]

        return mapped


class RAGFlowCtx:
    def __init__(self, connector: RAGFlowConnector):
        self.conn = connector


@asynccontextmanager
async def ragflow_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    ctx = RAGFlowCtx(RAGFlowConnector(base_url=BASE_URL))

    logging.info("RAGFlow FastMCP application started.")
    try:
        yield {"ragflow_ctx": ctx}
    finally:
        await ctx.conn.close()
        logging.info("RAGFlow FastMCP application shutting down...")


mcp = FastMCP(
    "ragflow-mcp-server",
    instructions=(
        "RAGFlow MCP tools expose dataset listing, document ingestion, and retrieval "
        "against the configured RAGFlow backend. In host mode each HTTP request must "
        "include an Authorization Bearer token or api_key header."
    ),
    lifespan=ragflow_lifespan,
    strict_input_validation=True,
)
AUTH_TOKEN_STATE_KEY = "ragflow_auth_token"


def _to_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="ignore")
    return str(value)


def _extract_token_from_headers(headers: Any) -> str | None:
    if not headers or not hasattr(headers, "get"):
        return None

    auth_keys = ("authorization", "Authorization", b"authorization", b"Authorization")
    for key in auth_keys:
        auth = headers.get(key)
        if not auth:
            continue
        auth_text = _to_text(auth).strip()
        if auth_text.lower().startswith("bearer "):
            token = auth_text[7:].strip()
            if token:
                return token

    api_key_keys = ("api_key", "x-api-key", "Api-Key", "X-API-Key", b"api_key", b"x-api-key", b"Api-Key", b"X-API-Key")
    for key in api_key_keys:
        token = headers.get(key)
        if token:
            token_text = _to_text(token).strip()
            if token_text:
                return token_text

    return None


def _extract_token_from_request(request: Any) -> str | None:
    if request is None:
        return None

    state = getattr(request, "state", None)
    if state is not None:
        token = getattr(state, AUTH_TOKEN_STATE_KEY, None)
        if token:
            return token

    token = _extract_token_from_headers(getattr(request, "headers", None))
    if token and state is not None:
        setattr(state, AUTH_TOKEN_STATE_KEY, token)

    return token


def _connector_from_context(ctx: Context) -> RAGFlowConnector:
    ragflow_ctx = ctx.lifespan_context.get("ragflow_ctx")
    if not ragflow_ctx:
        raise ToolError("Get RAGFlow Context failed.")
    return ragflow_ctx.conn


def _api_key_from_headers(headers: dict[str, str]) -> str:
    if MODE == LaunchMode.HOST:
        api_key = _extract_token_from_headers(headers) or ""
        if not api_key:
            raise ToolError("RAGFlow API key or Bearer token is required.")
        return api_key
    return HOST_API_KEY


def _runtime_from_request(ctx: Context, headers: dict[str, str]) -> tuple[RAGFlowConnector, str]:
    return _connector_from_context(ctx), _api_key_from_headers(headers)


def _raise_tool_error(exc: Exception):
    raise ToolError(RAGFlowConnector._exception_text(exc)) from exc


def _ingest_files_payload(files: list[DocumentIngestFile | dict]) -> list[dict]:
    payload = []
    for item in files:
        if isinstance(item, BaseModel):
            payload.append(item.model_dump(exclude_none=True))
        else:
            payload.append(item)
    return payload


async def list_tools(*, connector: RAGFlowConnector, api_key: str) -> list[types.Tool]:
    tools = [tool.to_mcp_tool() for tool in await mcp.list_tools(run_middleware=False)]
    try:
        dataset_description = await connector.list_datasets(api_key=api_key)
    except Exception as exc:
        logging.warning("Cannot load dataset descriptions for MCP tool list: %s", RAGFlowConnector._exception_text(exc))
        dataset_description = "\nDataset listing is currently unavailable; call list_datasets to retry."

    for tool in tools:
        tool.inputSchema = dereference_refs(tool.inputSchema)
        if tool.name == "retrieval":
            tool.description = (tool.description or "") + "\n\nAccessible datasets at listing time:\n" + dataset_description
    return tools


async def call_tool(
    name: str,
    arguments: dict,
    *,
    connector: RAGFlowConnector,
    api_key: str,
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        connector._mcp_error("arguments must be an object.")

    if name in {"list_datasets", "ragflow_list_datasets"}:
        try:
            connector._check_allowed_fields(arguments, {"page", "page_size", "id", "name"}, "arguments")
            page = connector._validate_int_argument(arguments.get("page", 1), "page", minimum=1)
            page_size = connector._validate_int_argument(arguments.get("page_size", 30), "page_size", minimum=1, maximum=1000)
            dataset_id = connector._validate_optional_string(arguments.get("id"), "id")
            dataset_name = connector._validate_optional_string(arguments.get("name"), "name")
        except ValueError as exc:
            connector._mcp_error(str(exc))
        return await connector.query_datasets(api_key=api_key, page=page, page_size=page_size, id=dataset_id, name=dataset_name)

    if name in {"document_ingest", "ragflow_document_ingest"}:
        try:
            connector._check_allowed_fields(arguments, {"dataset_id", "files"}, "arguments")
        except ValueError as exc:
            connector._mcp_error(str(exc))
        dataset_id = arguments.get("dataset_id", "")
        files = arguments.get("files", [])
        return await connector.document_ingest(api_key=api_key, dataset_id=dataset_id, files=files)

    if name in {"retrieval", "ragflow_retrieval"}:
        document_ids = arguments.get("document_ids", [])
        dataset_ids = arguments.get("dataset_ids", [])
        question = arguments.get("question", "")
        page = arguments.get("page", 1)
        page_size = arguments.get("page_size", 10)
        similarity_threshold = arguments.get("similarity_threshold", 0.2)
        vector_similarity_weight = arguments.get("vector_similarity_weight", 0.3)
        keyword = arguments.get("keyword", False)
        top_k = arguments.get("top_k", 1024)
        rerank_id = arguments.get("rerank_id")
        force_refresh = arguments.get("force_refresh", False)

        return await connector.retrieval(
            api_key=api_key,
            dataset_ids=dataset_ids,
            document_ids=document_ids,
            question=question,
            page=page,
            page_size=page_size,
            similarity_threshold=similarity_threshold,
            vector_similarity_weight=vector_similarity_weight,
            keyword=keyword,
            top_k=top_k,
            rerank_id=rerank_id,
            force_refresh=force_refresh,
        )
    raise ValueError(f"Tool not found: {name}")


@mcp.tool(
    name="list_datasets",
    description="List accessible RAGFlow datasets so users and agents can choose the target dataset_id before uploading documents.",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def ragflow_list_datasets(
    page: Annotated[int, Field(description="Page number for dataset pagination", ge=1)] = 1,
    page_size: Annotated[int, Field(description="Number of datasets to return per page", ge=1, le=1000)] = 30,
    id: Annotated[str | None, Field(description="Optional dataset ID filter")] = None,
    name: Annotated[str | None, Field(description="Optional dataset name filter")] = None,
    ctx: Context = CurrentContext(),
    headers: dict[str, str] = CurrentHeaders(),
) -> list[types.TextContent]:
    connector, api_key = _runtime_from_request(ctx, headers)
    try:
        return await connector.query_datasets(api_key=api_key, page=page, page_size=page_size, id=id, name=name)
    except Exception as exc:
        _raise_tool_error(exc)


@mcp.tool(
    name="document_ingest",
    description=(
        "Forward one or more documents to a RAGFlow dataset and schedule asynchronous parsing. "
        "Provide a configured DeerFlow file capability file_uri for each file. "
        "Arbitrary URLs, local paths, and raw sandbox paths are rejected. "
        "The tool returns immediately after forwarding and does not wait for parsing results."
    ),
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def ragflow_document_ingest(
    dataset_id: Annotated[str, Field(description="Target dataset ID")],
    files: Annotated[list[DocumentIngestFile], Field(description="Documents to forward. Each item must include a configured DeerFlow file capability file_uri. filename is optional when the source returns X-DeerFlow-Filename.", min_length=1)],
    ctx: Context = CurrentContext(),
    headers: dict[str, str] = CurrentHeaders(),
) -> list[types.TextContent]:
    connector, api_key = _runtime_from_request(ctx, headers)
    try:
        return await connector.document_ingest(api_key=api_key, dataset_id=dataset_id, files=_ingest_files_payload(files))
    except Exception as exc:
        _raise_tool_error(exc)


@mcp.tool(
    name="retrieval",
    description=(
        "Retrieve relevant chunks from the RAGFlow retrieve interface based on the question. "
        "Optionally specify dataset_ids to search specific datasets, or omit dataset_ids to "
        "search across all datasets accessible to the current RAGFlow API key. Optionally "
        "specify document_ids to restrict retrieval to specific documents."
    ),
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def ragflow_retrieval(
    question: Annotated[str, Field(description="The question or query to search for.")],
    dataset_ids: Annotated[list[str] | None, Field(description="Optional array of dataset IDs to search. If omitted or empty, all accessible datasets will be searched.")] = None,
    document_ids: Annotated[list[str] | None, Field(description="Optional array of document IDs to search within.")] = None,
    page: Annotated[int, Field(description="Page number for pagination", ge=1)] = 1,
    page_size: Annotated[int, Field(description="Number of results to return per page", ge=1, le=100)] = 10,
    similarity_threshold: Annotated[float, Field(description="Minimum similarity threshold for results", ge=0.0, le=1.0)] = 0.2,
    vector_similarity_weight: Annotated[float, Field(description="Weight for vector similarity vs term similarity", ge=0.0, le=1.0)] = 0.3,
    keyword: Annotated[bool, Field(description="Enable keyword-based search")] = False,
    top_k: Annotated[int, Field(description="Maximum results to consider before ranking", ge=1, le=1024)] = 1024,
    rerank_id: Annotated[str | None, Field(description="Optional reranking model identifier")] = None,
    force_refresh: Annotated[bool, Field(description="Set to true only if fresh dataset and document metadata is explicitly required. Otherwise, cached metadata is used.")] = False,
    ctx: Context = CurrentContext(),
    headers: dict[str, str] = CurrentHeaders(),
) -> list[types.TextContent]:
    connector, api_key = _runtime_from_request(ctx, headers)
    try:
        return await connector.retrieval(
            api_key=api_key,
            dataset_ids=dataset_ids or [],
            document_ids=document_ids or [],
            question=question,
            page=page,
            page_size=page_size,
            similarity_threshold=similarity_threshold,
            vector_similarity_weight=vector_similarity_weight,
            keyword=keyword,
            top_k=top_k,
            rerank_id=rerank_id,
            force_refresh=force_refresh,
        )
    except Exception as exc:
        _raise_tool_error(exc)


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        if path.startswith("/messages/") or path.startswith("/sse") or path.startswith("/mcp"):
            headers = dict(scope["headers"])
            token = _extract_token_from_headers(headers)

            if not token:
                response = JSONResponse({"error": "Missing or invalid authorization header"}, status_code=401)
                await response(scope, receive, send)
                return
            scope.setdefault("state", {})[AUTH_TOKEN_STATE_KEY] = token

        await self.app(scope, receive, send)


def _http_middleware():
    return [Middleware(AuthMiddleware)] if MODE == LaunchMode.HOST else []


def create_starlette_app():
    middleware = _http_middleware()
    if TRANSPORT_STREAMABLE_HTTP_ENABLED:
        streamable_http_app = mcp.http_app(
            path="/mcp",
            transport="http",
            middleware=middleware if not TRANSPORT_SSE_ENABLED else [],
            json_response=JSON_RESPONSE,
            stateless_http=JSON_RESPONSE,
        )
    else:
        streamable_http_app = None

    if TRANSPORT_SSE_ENABLED:
        sse_app = mcp.http_app(
            path="/sse",
            transport="sse",
            middleware=middleware if not TRANSPORT_STREAMABLE_HTTP_ENABLED else [],
        )
    else:
        sse_app = None

    if streamable_http_app and sse_app:
        combined = create_base_app(
            routes=[*streamable_http_app.routes, *sse_app.routes],
            middleware=middleware,
            debug=False,
            lifespan=combine_lifespans(streamable_http_app.lifespan, sse_app.lifespan),
        )
        combined.state.fastmcp_server = mcp
        combined.state.path = "/mcp,/sse"
        return combined

    if streamable_http_app:
        return streamable_http_app
    if sse_app:
        return sse_app

    return mcp.http_app(path="/mcp", transport="http", middleware=middleware, json_response=JSON_RESPONSE, stateless_http=JSON_RESPONSE)


@click.command()
@click.option("--base-url", type=str, default="http://127.0.0.1:9380", help="API base URL for RAGFlow backend")
@click.option("--host", type=str, default="127.0.0.1", help="Host to bind the RAGFlow MCP server")
@click.option("--port", type=int, default=9382, help="Port to bind the RAGFlow MCP server")
@click.option(
    "--mode",
    type=click.Choice(["self-host", "host"]),
    default="self-host",
    help=("Launch mode:\n  self-host: run MCP for a single tenant (requires --api-key)\n  host: multi-tenant mode, users must provide Authorization headers"),
)
@click.option("--api-key", type=str, default="", help="API key to use when in self-host mode")
@click.option(
    "--transport-sse-enabled/--no-transport-sse-enabled",
    default=True,
    help="Enable or disable legacy SSE transport mode (default: enabled)",
)
@click.option(
    "--transport-streamable-http-enabled/--no-transport-streamable-http-enabled",
    default=True,
    help="Enable or disable streamable-http transport mode (default: enabled)",
)
@click.option(
    "--json-response/--no-json-response",
    default=True,
    help="Enable or disable JSON response mode for streamable-http (default: enabled)",
)
def main(base_url, host, port, mode, api_key, transport_sse_enabled, transport_streamable_http_enabled, json_response):
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()

    def parse_bool_flag(key: str, default: bool) -> bool:
        val = os.environ.get(key, str(default))
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    global BASE_URL, HOST, PORT, MODE, HOST_API_KEY, TRANSPORT_SSE_ENABLED, TRANSPORT_STREAMABLE_HTTP_ENABLED, JSON_RESPONSE, FILE_URI_ALLOWED_BASE_URLS
    BASE_URL = os.environ.get("RAGFLOW_MCP_BASE_URL", base_url)
    HOST = os.environ.get("RAGFLOW_MCP_HOST", host)
    PORT = os.environ.get("RAGFLOW_MCP_PORT", str(port))
    MODE = os.environ.get("RAGFLOW_MCP_LAUNCH_MODE", mode)
    HOST_API_KEY = os.environ.get("RAGFLOW_MCP_HOST_API_KEY", api_key)
    FILE_URI_ALLOWED_BASE_URLS = os.environ.get("RAGFLOW_MCP_FILE_URI_ALLOWED_BASE_URLS", "")
    TRANSPORT_SSE_ENABLED = parse_bool_flag("RAGFLOW_MCP_TRANSPORT_SSE_ENABLED", transport_sse_enabled)
    TRANSPORT_STREAMABLE_HTTP_ENABLED = parse_bool_flag("RAGFLOW_MCP_TRANSPORT_STREAMABLE_ENABLED", transport_streamable_http_enabled)
    JSON_RESPONSE = parse_bool_flag("RAGFLOW_MCP_JSON_RESPONSE", json_response)

    if MODE == LaunchMode.SELF_HOST and not HOST_API_KEY:
        raise click.UsageError("--api-key is required when --mode is 'self-host'")

    if not TRANSPORT_STREAMABLE_HTTP_ENABLED and JSON_RESPONSE:
        JSON_RESPONSE = False

    print(
        r"""
__  __  ____ ____       ____  _____ ______     _______ ____
|  \/  |/ ___|  _ \     / ___|| ____|  _ \ \   / / ____|  _ \
| |\/| | |   | |_) |    \___ \|  _| | |_) \ \ / /|  _| | |_) |
| |  | | |___|  __/      ___) | |___|  _ < \ V / | |___|  _ <
|_|  |_|\____|_|        |____/|_____|_| \_\ \_/  |_____|_| \_\
        """,
        flush=True,
    )
    print(f"MCP launch mode: {MODE}", flush=True)
    print(f"MCP host: {HOST}", flush=True)
    print(f"MCP port: {PORT}", flush=True)
    print(f"MCP base_url: {BASE_URL}", flush=True)

    if not any([TRANSPORT_SSE_ENABLED, TRANSPORT_STREAMABLE_HTTP_ENABLED]):
        print("At least one transport should be enabled, enable streamable-http automatically", flush=True)
        TRANSPORT_STREAMABLE_HTTP_ENABLED = True

    if TRANSPORT_SSE_ENABLED:
        print("SSE transport enabled: yes", flush=True)
        print("SSE endpoint available at /sse", flush=True)
    else:
        print("SSE transport enabled: no", flush=True)

    if TRANSPORT_STREAMABLE_HTTP_ENABLED:
        print("Streamable HTTP transport enabled: yes", flush=True)
        print("Streamable HTTP endpoint available at /mcp", flush=True)
        if JSON_RESPONSE:
            print("Streamable HTTP mode: JSON response enabled", flush=True)
        else:
            print("Streamable HTTP mode: SSE over HTTP enabled", flush=True)
    else:
        print("Streamable HTTP transport enabled: no", flush=True)
        if JSON_RESPONSE:
            print("Warning: --json-response ignored because streamable transport is disabled.", flush=True)

    uvicorn.run(
        create_starlette_app(),
        host=HOST,
        port=int(PORT),
    )


if __name__ == "__main__":
    """
    Launch examples:

    1. Self-host mode with both SSE and Streamable HTTP (in JSON response mode) enabled (default):
        uv run mcp/server/server.py --host=127.0.0.1 --port=9382 \
            --base-url=http://127.0.0.1:9380 \
            --mode=self-host --api-key=ragflow-xxxxx

    2. Host mode (multi-tenant, clients must provide Authorization headers):
        uv run mcp/server/server.py --host=127.0.0.1 --port=9382 \
            --base-url=http://127.0.0.1:9380 \
            --mode=host

    3. Disable legacy SSE (only streamable HTTP will be active):
        uv run mcp/server/server.py --no-transport-sse-enabled \
            --mode=self-host --api-key=ragflow-xxxxx

    4. Disable streamable HTTP (only legacy SSE will be active):
        uv run mcp/server/server.py --no-transport-streamable-http-enabled \
            --mode=self-host --api-key=ragflow-xxxxx

    5. Use streamable HTTP with SSE-style events (disable JSON response):
        uv run mcp/server/server.py --transport-streamable-http-enabled --no-json-response \
            --mode=self-host --api-key=ragflow-xxxxx

    6. Disable both transports (for testing):
        uv run mcp/server/server.py --no-transport-sse-enabled --no-transport-streamable-http-enabled \
            --mode=self-host --api-key=ragflow-xxxxx
    """
    main()
