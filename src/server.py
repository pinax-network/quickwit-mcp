#!/usr/bin/env python3
"""
Quickwit MCP Server - generated from Quickwit's REST OpenAPI specification.
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from src.utils import filter_openapi_spec_for_readonly, patch_openapi_spec_for_keywords

try:
    __version__ = version("quickwit-mcp")
except PackageNotFoundError:
    __version__ = "unknown"


VALID_TOOL_MODES = {"full", "readonly"}
DEFAULT_QUICKWIT_BASE_URL = "http://localhost:7280"


@dataclass(frozen=True)
class Settings:
    active_session_ttl: int
    mcp_endpoint_path: str
    mcp_host: str
    mcp_port: int
    mcp_transport: str
    mcp_user_agent: str
    openapi_spec_url: str
    quickwit_base_url: str
    tool_mode: str


def build_settings(argv: list[str] | None = None) -> Settings:
    parser = argparse.ArgumentParser(description="Run Quickwit MCP server")
    parser.add_argument("--quickwit-base-url", default=os.getenv("QUICKWIT_BASE_URL", DEFAULT_QUICKWIT_BASE_URL))
    parser.add_argument("--openapi-spec-url", default=os.getenv("OPENAPI_SPEC_URL"))
    parser.add_argument("--tool-mode", choices=sorted(VALID_TOOL_MODES), default=os.getenv("QUICKWIT_TOOL_MODE", "full"))
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "8080")))
    parser.add_argument("--endpoint-path", default=os.getenv("MCP_ENDPOINT_PATH", "/"))
    args = parser.parse_args(argv)

    quickwit_base_url = args.quickwit_base_url.rstrip("/")
    openapi_spec_url = args.openapi_spec_url or f"{quickwit_base_url}/openapi.json"
    tool_mode = args.tool_mode.lower()

    if tool_mode not in VALID_TOOL_MODES:
        raise ValueError(f"Invalid QUICKWIT_TOOL_MODE={tool_mode!r}. Expected one of: {sorted(VALID_TOOL_MODES)}")

    return Settings(
        active_session_ttl=int(os.getenv("ACTIVE_SESSION_TTL", "600")),
        mcp_endpoint_path=args.endpoint_path,
        mcp_host=args.host,
        mcp_port=args.port,
        mcp_transport="streamable-http",
        mcp_user_agent=os.getenv("MCP_USER_AGENT", f"quickwit-mcp/{__version__}"),
        openapi_spec_url=openapi_spec_url,
        quickwit_base_url=quickwit_base_url,
        tool_mode=tool_mode,
    )


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)8s] %(message)s (%(filename)s:%(lineno)s)",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

MCP_VERSION = __version__
OPENAPI_SPEC: dict[str, Any] | None = None
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


def fetch_openapi_spec(settings: Settings) -> dict[str, Any] | None:
    """Fetch and patch OpenAPI spec from Quickwit."""
    logger.info("Fetching OpenAPI spec from %s", settings.openapi_spec_url)
    try:
        response = httpx.get(settings.openapi_spec_url, timeout=10.0, headers={"user-agent": settings.mcp_user_agent})
        response.raise_for_status()
        spec = response.json()

        if not isinstance(spec, dict) or "openapi" not in spec or "paths" not in spec:
            logger.error("Invalid OpenAPI spec received")
            return None

        logger.info("Successfully loaded OpenAPI spec with %s endpoints", len(spec.get("paths", {})))

        patched_spec = patch_openapi_spec_for_keywords(spec)
        if settings.tool_mode == "readonly":
            patched_spec = filter_openapi_spec_for_readonly(patched_spec)
            logger.info("Readonly mode enabled; exposing %s OpenAPI paths", len(patched_spec.get("paths", {})))

        return patched_spec
    except httpx.HTTPStatusError as exc:
        logger.error("HTTP error fetching OpenAPI spec: %s - %s", exc.response.status_code, exc.response.text)
        return None
    except httpx.RequestError as exc:
        logger.error("Network error fetching OpenAPI spec: %s", exc)
        return None
    except ValueError as exc:
        logger.error("Invalid JSON fetching OpenAPI spec: %s", exc)
        return None
    except Exception as exc:
        logger.error("Unexpected error fetching OpenAPI spec: %s", exc)
        return None


def create_mcp_from_openapi(spec: dict[str, Any], client: httpx.AsyncClient, settings: Settings) -> FastMCP | None:
    """Create MCP server from OpenAPI specification using existing HTTP client."""
    try:
        mcp = FastMCP.from_openapi(client=client, openapi_spec=spec, name="Quickwit MCP", version=MCP_VERSION)

        @mcp.custom_route("/health", methods=["GET"])
        async def _(_: Request) -> PlainTextResponse:
            return PlainTextResponse("OK")

        mcp.add_middleware(SessionTrackingMiddleware(settings))
        return mcp
    except Exception as exc:
        logger.error("Failed to create MCP server from OpenAPI spec: %s", exc)
        return None


async def main(argv: list[str] | None = None):
    global HTTP_CLIENT, MCP_INSTANCE, OPENAPI_SPEC

    try:
        settings = build_settings(argv)
    except Exception as exc:
        logger.error("Invalid configuration: %s", exc)
        sys.exit(2)

    logger.info("Initializing Quickwit MCP Server...")
    logger.info("Quickwit base URL: %s", settings.quickwit_base_url)
    logger.info("Tool mode: %s", settings.tool_mode)

    OPENAPI_SPEC = fetch_openapi_spec(settings)
    if not OPENAPI_SPEC:
        logger.error("Failed to load OpenAPI spec. Make sure Quickwit is reachable at %s", settings.quickwit_base_url)
        sys.exit(1)

    HTTP_CLIENT = httpx.AsyncClient(
        base_url=settings.quickwit_base_url,
        timeout=30.0,
        headers={"user-agent": settings.mcp_user_agent},
    )
    logger.info("Created persistent HTTP client with User-Agent: %s", settings.mcp_user_agent)

    MCP_INSTANCE = create_mcp_from_openapi(OPENAPI_SPEC, HTTP_CLIENT, settings)
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


if __name__ == "__main__":
    asyncio.run(main())
