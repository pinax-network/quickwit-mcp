import httpx
import pytest
from fastmcp.client import Client
from fastmcp.exceptions import ToolError

from src.server import MAX_SEARCH_HITS, MAX_SPLITS_LIMIT, build_settings, create_mcp, _validate_index_id


def test_build_settings_defaults():
    settings = build_settings([])

    assert settings.quickwit_base_url == "http://localhost:7280"
    assert settings.mcp_port == 8080


def test_build_settings_accepts_cli_overrides():
    settings = build_settings([
        "--quickwit-base-url",
        "http://quickwit.vpn:7280/",
        "--port",
        "9090",
    ])

    assert settings.quickwit_base_url == "http://quickwit.vpn:7280"
    assert settings.mcp_port == 9090


def quickwit_transport(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v1/indexes":
        return httpx.Response(
            200,
            json=[
                {
                    "version": "0.9",
                    "index_id": "k8s-logs",
                    "index_uri": "s3://quickwit/indexes/k8s-logs",
                    "doc_mapping": {"timestamp_field": "timestamp"},
                    "search_settings": {"default_search_fields": ["message"]},
                    "extra": {"nested": "value"},
                }
            ],
        )
    if request.url.path == "/api/v1/version":
        return httpx.Response(200, json={"build": {"version": "0.8.0-nightly"}})
    if request.url.path == "/api/v1/indexes/k8s-logs/describe":
        return httpx.Response(200, json={"index_id": "k8s-logs"})
    if request.url.path == "/api/v1/k8s-logs/search":
        return httpx.Response(200, json={"num_hits": 0, "hits": [], "errors": []})
    if request.url.path == "/api/v1/indexes/k8s-logs/splits":
        return httpx.Response(200, json={"offset": 0, "size": 0, "splits": []})
    if request.url.path == "/api/v1/k8s-logs/delete-tasks":
        return httpx.Response(200, json=[])
    return httpx.Response(404, json={"message": f"unexpected path {request.url.path}"})


@pytest.mark.asyncio
async def test_create_mcp_exposes_only_curated_tools():
    settings = build_settings([])
    client = httpx.AsyncClient(base_url="http://quickwit.example", transport=httpx.MockTransport(quickwit_transport))
    mcp = create_mcp(client, settings)

    assert mcp is not None
    async with Client(transport=mcp) as mcp_client:
        tools = await mcp_client.list_tools()

    tool_names = {tool.name for tool in tools}
    assert tool_names == {"describe_index", "get_delete_tasks", "list_indexes", "list_splits", "search", "version"}
    assert "metrics" not in tool_names
    assert "debug" not in tool_names
    assert "node_config" not in tool_names

    await client.aclose()


@pytest.mark.asyncio
async def test_list_indexes_simplifies_real_metadata_shape():
    settings = build_settings([])
    client = httpx.AsyncClient(base_url="http://quickwit.example", transport=httpx.MockTransport(quickwit_transport))
    mcp = create_mcp(client, settings)

    assert mcp is not None
    async with Client(transport=mcp) as mcp_client:
        result = await mcp_client.call_tool("list_indexes", {})

    assert result.structured_content == {
        "indexes": [
            {
                "index_id": "k8s-logs",
                "index_uri": "s3://quickwit/indexes/k8s-logs",
                "version": "0.9",
                "timestamp_field": "timestamp",
                "default_search_fields": ["message"],
            }
        ]
    }

    await client.aclose()


@pytest.mark.asyncio
async def test_search_caps_max_hits():
    captured = {}

    def transport(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"num_hits": 0, "hits": [], "errors": []})

    settings = build_settings([])
    client = httpx.AsyncClient(base_url="http://quickwit.example", transport=httpx.MockTransport(transport))
    mcp = create_mcp(client, settings)

    assert mcp is not None
    async with Client(transport=mcp) as mcp_client:
        await mcp_client.call_tool("search", {"index_id": "k8s-logs", "query": "*", "max_hits": 1000})

    assert f'"max_hits":{MAX_SEARCH_HITS}' in captured["body"].replace(" ", "")
    await client.aclose()


@pytest.mark.asyncio
async def test_list_splits_caps_limit():
    captured = {}

    def transport(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"offset": 0, "size": 0, "splits": []})

    settings = build_settings([])
    client = httpx.AsyncClient(base_url="http://quickwit.example", transport=httpx.MockTransport(transport))
    mcp = create_mcp(client, settings)

    assert mcp is not None
    async with Client(transport=mcp) as mcp_client:
        await mcp_client.call_tool("list_splits", {"index_id": "k8s-logs", "limit": 1000})

    assert f"limit={MAX_SPLITS_LIMIT}" in captured["url"]
    await client.aclose()


@pytest.mark.parametrize("index_id", ["k8s-logs", "logs_2026", "tenant.prod.logs", "logs-01"])
def test_validate_index_id_accepts_safe_values(index_id):
    _validate_index_id(index_id)


@pytest.mark.parametrize(
    "index_id",
    ["", "../logs", "logs/2026", "logs?format=json", "logs#fragment", "logs%2F2026", "logs 2026"],
)
def test_validate_index_id_rejects_unsafe_values(index_id):
    with pytest.raises(ToolError, match="index_id is invalid"):
        _validate_index_id(index_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quickwit_version_available(quickwit_base_url: str | None):
    if not quickwit_base_url:
        pytest.skip("QUICKWIT_BASE_URL is not set")

    async with httpx.AsyncClient(base_url=quickwit_base_url.rstrip("/"), timeout=10.0) as client:
        response = await client.get("/api/v1/version")

    response.raise_for_status()
    assert isinstance(response.json(), dict)
