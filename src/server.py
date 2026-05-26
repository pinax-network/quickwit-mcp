#!/usr/bin/env python3
"""
Quickwit MCP Server exposing curated readonly tools.
"""

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext
from starlette.requests import Request
from starlette.responses import PlainTextResponse

try:
    __version__ = version("quickwit-mcp")
except PackageNotFoundError:
    __version__ = "unknown"


DEFAULT_QUICKWIT_BASE_URL = "http://localhost:7280"
INDEX_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
MAX_SEARCH_HITS = 100
MAX_SPLITS_LIMIT = 100


@dataclass(frozen=True)
class Settings:
    active_session_ttl: int
    mcp_endpoint_path: str
    mcp_host: str
    mcp_port: int
    mcp_rate_limit_burst: int
    mcp_rate_limit_rps: float
    mcp_response_limit_bytes: int
    mcp_transport: str
    mcp_user_agent: str
    quickwit_base_url: str


def build_settings(argv: list[str] | None = None) -> Settings:
    parser = argparse.ArgumentParser(description="Run Quickwit MCP server")
    parser.add_argument("--quickwit-base-url", default=os.getenv("QUICKWIT_BASE_URL", DEFAULT_QUICKWIT_BASE_URL))
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "8080")))
    parser.add_argument("--endpoint-path", default=os.getenv("MCP_ENDPOINT_PATH", "/"))
    args = parser.parse_args(argv)

    quickwit_base_url = args.quickwit_base_url.rstrip("/")

    return Settings(
        active_session_ttl=int(os.getenv("ACTIVE_SESSION_TTL", "600")),
        mcp_endpoint_path=args.endpoint_path,
        mcp_host=args.host,
        mcp_port=args.port,
        mcp_rate_limit_burst=int(os.getenv("MCP_RATE_LIMIT_BURST", "20")),
        mcp_rate_limit_rps=float(os.getenv("MCP_RATE_LIMIT_RPS", "5.0")),
        mcp_response_limit_bytes=int(os.getenv("MCP_RESPONSE_LIMIT_BYTES", "500000")),
        mcp_transport="streamable-http",
        mcp_user_agent=os.getenv("MCP_USER_AGENT", f"quickwit-mcp/{__version__}"),
        quickwit_base_url=quickwit_base_url,
    )


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)8s] %(message)s (%(filename)s:%(lineno)s)",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

MCP_VERSION = __version__
MCP_INSTANCE: FastMCP | None = None
HTTP_CLIENT: httpx.AsyncClient | None = None
ACTIVE_SESSIONS: dict[str, float] = {}


class SessionTrackingMiddleware(Middleware):
    def __init__(self, settings: Settings):
        self.settings = settings

    async def on_message(self, context: MiddlewareContext, call_next):
        try:
            request = get_http_request()
            headers = request.headers.mutablecopy()
            original_user_agent = headers.get("user-agent", "")
            headers.update({"user-agent": f"{self.settings.mcp_user_agent} {original_user_agent}".strip()})
            request._headers = headers
        except Exception as exc:
            logger.debug("Could not update User-Agent for client request: %s", exc)

        return await call_next(context)

    async def on_request(self, context: MiddlewareContext, call_next):
        if context.fastmcp_context:
            try:
                session_id = context.fastmcp_context.session_id
                _prune_expired_sessions(self.settings.active_session_ttl)
                ACTIVE_SESSIONS[session_id] = time.monotonic()
                logger.debug("Tracking session (total: %s)", len(ACTIVE_SESSIONS))
            except Exception as exc:
                logger.debug("Exception while tracking session: %s", exc)

        return await call_next(context)


async def quickwit_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    """Run a Quickwit HTTP request and return JSON or text with clean tool errors."""
    try:
        response = await client.request(method, path, params=_strip_none(params), json=json_body)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        message = _extract_error_message(exc.response)
        logger.warning("Quickwit HTTP error %s %s: %s", method, path, message)
        raise ToolError(f"Quickwit returned HTTP {exc.response.status_code}: {message}") from exc
    except httpx.RequestError as exc:
        logger.warning("Quickwit request failed %s %s: %s", method, path, exc)
        raise ToolError(f"Quickwit request failed: {exc}") from exc

    if not response.content:
        return None

    content_type = response.headers.get("content-type", "")
    if "json" not in content_type:
        return response.text

    try:
        return response.json()
    except ValueError as exc:
        logger.warning("Invalid JSON from Quickwit %s %s: %s", method, path, exc)
        raise ToolError("Quickwit returned invalid JSON") from exc


def create_mcp(client: httpx.AsyncClient, settings: Settings) -> FastMCP | None:
    """Create curated MCP server using a small stable Quickwit tool allowlist."""
    try:
        mcp = FastMCP(name="Quickwit MCP", version=MCP_VERSION)

        @mcp.custom_route("/health", methods=["GET"])
        async def _(_: Request) -> PlainTextResponse:
            return PlainTextResponse("OK")

        @mcp.tool
        async def version() -> dict[str, Any]:
            """Return Quickwit node version and runtime information."""
            return await quickwit_request(client, "GET", "/api/v1/version")

        @mcp.tool
        async def list_indexes() -> dict[str, list[dict[str, Any]]]:
            """List Quickwit indexes with simplified metadata."""
            metadata = await quickwit_request(client, "GET", "/api/v1/indexes")
            if not isinstance(metadata, list):
                raise ToolError("Quickwit returned unexpected index metadata")
            return {"indexes": [_simplify_index_metadata(item) for item in metadata if isinstance(item, dict)]}

        @mcp.tool
        async def describe_index(index_id: str) -> dict[str, Any]:
            """Describe one Quickwit index."""
            _validate_index_id(index_id)
            return await quickwit_request(client, "GET", f"/api/v1/indexes/{index_id}/describe")

        @mcp.tool
        async def search(
            index_id: str,
            query: str,
            max_hits: int = 20,
            start_offset: int = 0,
            start_timestamp: int | None = None,
            end_timestamp: int | None = None,
            search_field: list[str] | None = None,
            snippet_fields: list[str] | None = None,
            sort_by: dict[str, Any] | None = None,
            aggs: dict[str, Any] | None = None,
            count_all: bool = False,
            allow_failed_splits: bool = False,
        ) -> dict[str, Any]:
            """Search a Quickwit index. max_hits is capped at 100."""
            _validate_index_id(index_id)
            query = query.strip()
            if not query:
                raise ToolError("query must not be empty")
            max_hits = _clamp_int(max_hits, minimum=0, maximum=MAX_SEARCH_HITS, name="max_hits")
            start_offset = _clamp_int(start_offset, minimum=0, maximum=1_000_000, name="start_offset")
            body = _strip_none({
                "query": query,
                "max_hits": max_hits,
                "start_offset": start_offset,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "search_field": search_field,
                "snippet_fields": snippet_fields,
                "sort_by": sort_by,
                "aggs": aggs,
                "count_all": count_all,
                "allow_failed_splits": allow_failed_splits,
            })
            return await quickwit_request(client, "POST", f"/api/v1/{index_id}/search", json_body=body)

        @mcp.tool
        async def list_splits(
            index_id: str,
            offset: int = 0,
            limit: int = 20,
            split_states: list[str] | None = None,
            start_timestamp: int | None = None,
            end_timestamp: int | None = None,
            end_create_timestamp: int | None = None,
        ) -> dict[str, Any]:
            """List splits for a Quickwit index. limit is capped at 100."""
            _validate_index_id(index_id)
            offset = _clamp_int(offset, minimum=0, maximum=1_000_000, name="offset")
            limit = _clamp_int(limit, minimum=0, maximum=MAX_SPLITS_LIMIT, name="limit")
            params = {
                "offset": offset,
                "limit": limit,
                "split_states": split_states,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "end_create_timestamp": end_create_timestamp,
            }
            return await quickwit_request(client, "GET", f"/api/v1/indexes/{index_id}/splits", params=params)

        @mcp.tool
        async def get_delete_tasks(index_id: str) -> dict[str, list[dict[str, Any]]]:
            """List delete tasks for a Quickwit index."""
            _validate_index_id(index_id)
            result = await quickwit_request(client, "GET", f"/api/v1/{index_id}/delete-tasks")
            if not isinstance(result, list):
                raise ToolError("Quickwit returned unexpected delete tasks")
            return {"delete_tasks": result}

        _add_optional_middleware(mcp, settings)
        mcp.add_middleware(SessionTrackingMiddleware(settings))
        return mcp
    except Exception as exc:
        logger.error("Failed to create MCP server: %s", exc)
        return None


async def main(argv: list[str] | None = None):
    global HTTP_CLIENT, MCP_INSTANCE

    try:
        settings = build_settings(argv)
    except Exception as exc:
        logger.error("Invalid configuration: %s", exc)
        sys.exit(2)

    logger.info("Initializing Quickwit MCP Server...")
    logger.info("Quickwit base URL: %s", settings.quickwit_base_url)

    HTTP_CLIENT = httpx.AsyncClient(
        base_url=settings.quickwit_base_url,
        timeout=30.0,
        headers={"user-agent": settings.mcp_user_agent},
    )
    logger.info("Created persistent HTTP client with User-Agent: %s", settings.mcp_user_agent)

    MCP_INSTANCE = create_mcp(HTTP_CLIENT, settings)
    if not MCP_INSTANCE:
        logger.error("Failed to create initial MCP instance")
        await HTTP_CLIENT.aclose()
        sys.exit(1)

    logger.info("Starting Quickwit MCP server on %s:%s", settings.mcp_host, settings.mcp_port)
    try:
        await MCP_INSTANCE.run_async(
            transport=settings.mcp_transport,
            host=settings.mcp_host,
            port=settings.mcp_port,
            path=settings.mcp_endpoint_path,
        )
    finally:
        if HTTP_CLIENT:
            await HTTP_CLIENT.aclose()
            logger.info("HTTP client closed")


def _prune_expired_sessions(ttl_seconds: int) -> None:
    now = time.monotonic()
    for session_id, last_seen in list(ACTIVE_SESSIONS.items()):
        if now - last_seen > ttl_seconds:
            ACTIVE_SESSIONS.pop(session_id, None)


def _add_optional_middleware(mcp: FastMCP, settings: Settings) -> None:
    middleware_specs = (
        (
            "fastmcp.server.middleware.error_handling",
            "ErrorHandlingMiddleware",
            {"include_traceback": False, "transform_errors": True},
        ),
        (
            "fastmcp.server.middleware.rate_limiting",
            "RateLimitingMiddleware",
            {"max_requests_per_second": settings.mcp_rate_limit_rps, "burst_capacity": settings.mcp_rate_limit_burst},
        ),
        (
            "fastmcp.server.middleware.response_limiting",
            "ResponseLimitingMiddleware",
            {"max_size": settings.mcp_response_limit_bytes},
        ),
        (
            "fastmcp.server.middleware.logging",
            "StructuredLoggingMiddleware",
            {"include_payloads": False},
        ),
    )
    for module_name, class_name, kwargs in middleware_specs:
        middleware_class = _load_class(module_name, class_name)
        if middleware_class is None:
            logger.info("Skipping unavailable FastMCP middleware: %s", class_name)
            continue
        try:
            mcp.add_middleware(middleware_class(**kwargs))
        except TypeError as exc:
            logger.info("Skipping incompatible FastMCP middleware %s: %s", class_name, exc)


def _load_class(module_name: str, class_name: str) -> type | None:
    try:
        module = import_module(module_name)
    except ImportError:
        return None
    return getattr(module, class_name, None)


def _strip_none(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {key: item for key, item in value.items() if item is not None and item != []}


def _extract_error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(data, dict):
        message = data.get("message") or data.get("error") or data
        return str(message)[:500]
    return str(data)[:500]


def _clamp_int(value: int, *, minimum: int, maximum: int, name: str) -> int:
    if value < minimum:
        raise ToolError(f"{name} must be >= {minimum}")
    return min(value, maximum)


def _validate_index_id(index_id: str) -> None:
    if not INDEX_ID_PATTERN.fullmatch(index_id):
        raise ToolError("index_id is invalid")


def _simplify_index_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    doc_mapping = metadata.get("doc_mapping") if isinstance(metadata.get("doc_mapping"), dict) else {}
    search_settings = metadata.get("search_settings") if isinstance(metadata.get("search_settings"), dict) else {}
    return {
        "index_id": metadata.get("index_id"),
        "index_uri": metadata.get("index_uri"),
        "version": metadata.get("version"),
        "timestamp_field": doc_mapping.get("timestamp_field"),
        "default_search_fields": search_settings.get("default_search_fields", []),
    }


if __name__ == "__main__":
    asyncio.run(main())
