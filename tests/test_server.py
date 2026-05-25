import httpx
import pytest
from fastmcp.client import Client

from src.server import build_settings, create_mcp_from_openapi
from src.utils import filter_openapi_spec_for_readonly, patch_openapi_spec_for_keywords


def minimal_openapi_spec() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Quickwit", "version": "0.1.0"},
        "paths": {
            "/api/v1/indexes": {
                "get": {
                    "operationId": "listIndexes",
                    "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "array", "items": {"type": "object"}}}}}},
                },
                "post": {
                    "operationId": "createIndex",
                    "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "OK"}},
                },
            },
            "/api/v1/{index_id}/search": {
                "post": {
                    "operationId": "searchIndex",
                    "parameters": [
                        {"name": "index_id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            },
            "/api/v1/indexes/{index_id}": {
                "delete": {
                    "operationId": "deleteIndex",
                    "parameters": [
                        {"name": "index_id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "OK"}},
                }
            },
        },
    }


def test_build_settings_defaults():
    settings = build_settings([])

    assert settings.quickwit_base_url == "http://localhost:7280"
    assert settings.openapi_spec_url == "http://localhost:7280/openapi.json"
    assert settings.tool_mode == "full"


def test_build_settings_accepts_cli_overrides():
    settings = build_settings([
        "--quickwit-base-url",
        "http://quickwit.vpn:7280/",
        "--tool-mode",
        "readonly",
        "--port",
        "9090",
    ])

    assert settings.quickwit_base_url == "http://quickwit.vpn:7280"
    assert settings.openapi_spec_url == "http://quickwit.vpn:7280/openapi.json"
    assert settings.tool_mode == "readonly"
    assert settings.mcp_port == 9090


def test_patch_openapi_spec_for_keywords():
    spec = {"components": {"schemas": {"Example": {"properties": {"from": {"type": "string"}}}}}}

    patched = patch_openapi_spec_for_keywords(spec)

    properties = patched["components"]["schemas"]["Example"]["properties"]
    assert "from" not in properties
    assert "from_" in properties
    assert "from" in spec["components"]["schemas"]["Example"]["properties"]


def test_filter_openapi_spec_for_readonly_removes_mutations():
    filtered = filter_openapi_spec_for_readonly(minimal_openapi_spec())

    assert "get" in filtered["paths"]["/api/v1/indexes"]
    assert "post" not in filtered["paths"]["/api/v1/indexes"]
    assert "post" in filtered["paths"]["/api/v1/{index_id}/search"]
    assert "/api/v1/indexes/{index_id}" not in filtered["paths"]


@pytest.mark.asyncio
async def test_create_mcp_from_openapi_exposes_tools():
    settings = build_settings([])
    client = httpx.AsyncClient(base_url="http://quickwit.example", transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    mcp = create_mcp_from_openapi(minimal_openapi_spec(), client, settings)

    assert mcp is not None
    async with Client(transport=mcp) as mcp_client:
        tools = await mcp_client.list_tools()

    tool_names = {tool.name for tool in tools}
    assert "listIndexes" in tool_names
    assert "searchIndex" in tool_names

    await client.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quickwit_openapi_available(quickwit_base_url: str | None):
    if not quickwit_base_url:
        pytest.skip("QUICKWIT_BASE_URL is not set")

    async with httpx.AsyncClient(base_url=quickwit_base_url.rstrip("/"), timeout=10.0) as client:
        response = await client.get("/openapi.json")

    response.raise_for_status()
    spec = response.json()
    assert "openapi" in spec
    assert "paths" in spec
