import httpx
import pytest
from fastmcp.client import Client
from fastmcp.exceptions import ToolError

from src.server import (
    MAX_SEARCH_HITS,
    MAX_SPLITS_LIMIT,
    _validate_index_id,
    build_settings,
    create_mcp,
)


def test_build_settings_defaults():
    settings = build_settings([])

    assert settings.quickwit_base_url == "http://localhost:7280"
    assert settings.mcp_host == "127.0.0.1"
    assert settings.mcp_port == 8080


def test_build_settings_accepts_cli_overrides():
    settings = build_settings([
        "--quickwit-base-url",
        "http://quickwit.vpn:7280/",
        "--host",
        "0.0.0.0",
        "--port",
        "9090",
    ])

    assert settings.quickwit_base_url == "http://quickwit.vpn:7280"
    assert settings.mcp_host == "0.0.0.0"
    assert settings.mcp_port == 9090


def quickwit_transport(request: httpx.Request) -> httpx.Response:
    index_metadata = {
        "version": "0.9",
        "index_uid": "sample-index:01KFKY0ZE5WZPZY50NHQTMT3RZ",
        "index_config": {
            "version": "0.9",
            "index_id": "sample-index",
            "index_uri": "s3://quickwit/indexes/sample-index",
            "doc_mapping": {
                "timestamp_field": "timestamp",
                "field_mappings": [
                    {"name": "timestamp", "type": "datetime"},
                    {"name": "message", "type": "text"},
                    {"name": "status", "type": "text"},
                    {"name": "service.name", "type": "text"},
                    {"name": "host.name", "type": "text"},
                ],
            },
            "search_settings": {"default_search_fields": ["message"]},
        },
        "checkpoint": {},
        "create_timestamp": 1777958213,
        "sources": [],
    }
    if request.url.path == "/api/v1/indexes":
        return httpx.Response(200, json=[index_metadata])
    if request.url.path == "/api/v1/indexes/sample-index":
        return httpx.Response(200, json=index_metadata)
    if request.url.path == "/api/v1/version":
        return httpx.Response(200, json={"build": {"version": "0.8.0-nightly"}})
    if request.url.path == "/api/v1/indexes/sample-index/describe":
        return httpx.Response(200, json={"index_id": "sample-index"})
    if request.url.path == "/api/v1/sample-index/search":
        return httpx.Response(
            200,
            json={
                "num_hits": 1,
                "elapsed_time_micros": 42,
                "hits": [
                    {
                        "timestamp": "2026-05-31T14:12:00Z",
                        "status": "INFO",
                        "message": "service started",
                        "service.name": "storage-api",
                        "host.name": "host-01",
                    }
                ],
                "errors": [],
            },
        )
    if request.url.path == "/api/v1/indexes/sample-index/splits":
        return httpx.Response(200, json={"offset": 0, "size": 0, "splits": []})
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
    assert tool_names == {
        "describe_index",
        "inspect_index",
        "list_indexes",
        "list_splits",
        "search",
        "search_logs",
        "version",
    }
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

    assert result.structured_content["indexes"][0]["summary"] == {
        "index_id": "sample-index",
        "index_uri": "s3://quickwit/indexes/sample-index",
        "version": "0.9",
        "timestamp_field": "timestamp",
        "default_search_fields": ["message"],
    }
    assert result.structured_content["indexes"][0]["raw_metadata"]["index_uid"] == (
        "sample-index:01KFKY0ZE5WZPZY50NHQTMT3RZ"
    )

    await client.aclose()


@pytest.mark.asyncio
async def test_list_indexes_extracts_index_id_from_index_uid_without_index_config():
    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/indexes":
            return httpx.Response(200, json=[{"version": "0.9", "index_uid": "sample-index:01ABC"}])
        return httpx.Response(404, json={"message": f"unexpected path {request.url.path}"})

    settings = build_settings([])
    client = httpx.AsyncClient(base_url="http://quickwit.example", transport=httpx.MockTransport(transport))
    mcp = create_mcp(client, settings)

    assert mcp is not None
    async with Client(transport=mcp) as mcp_client:
        result = await mcp_client.call_tool("list_indexes", {})

    assert result.structured_content == {
        "indexes": [
            {
                "summary": {
                    "index_id": "sample-index",
                    "index_uri": None,
                    "version": "0.9",
                    "timestamp_field": None,
                    "default_search_fields": [],
                },
                "raw_metadata": {"version": "0.9", "index_uid": "sample-index:01ABC"},
            }
        ]
    }

    await client.aclose()


@pytest.mark.asyncio
async def test_inspect_index_returns_quickwit_native_metadata():
    settings = build_settings([])
    client = httpx.AsyncClient(base_url="http://quickwit.example", transport=httpx.MockTransport(quickwit_transport))
    mcp = create_mcp(client, settings)

    assert mcp is not None
    async with Client(transport=mcp) as mcp_client:
        result = await mcp_client.call_tool("inspect_index", {"index_id": "sample-index"})

    content = result.structured_content
    assert content["index_id"] == "sample-index"
    assert content["index_uri"] == "s3://quickwit/indexes/sample-index"
    assert content["version"] == "0.9"
    assert content["timestamp_field"] == "timestamp"
    assert content["default_search_fields"] == ["message"]
    assert content["field_names"] == [
        "host.name",
        "message",
        "service.name",
        "status",
        "timestamp",
    ]
    assert "candidate_log_fields" not in content
    await client.aclose()


@pytest.mark.asyncio
async def test_search_logs_uses_rfc3339_time_and_compact_response():
    captured = {}

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/sample-index/search":
            captured["body"] = request.read().decode()
        return quickwit_transport(request)

    settings = build_settings([])
    client = httpx.AsyncClient(base_url="http://quickwit.example", transport=httpx.MockTransport(transport))
    mcp = create_mcp(client, settings)

    assert mcp is not None
    async with Client(transport=mcp) as mcp_client:
        result = await mcp_client.call_tool(
            "search_logs",
            {
                "index_id": "sample-index",
                "text": "storage-api",
                "start_time": "2026-05-31T14:00:00Z",
                "end_time": "2026-05-31T15:00:00Z",
            },
        )

    body = captured["body"].replace(" ", "")
    assert '"query":"storage-api"' in body
    assert '"sort_by":["-timestamp"]' in body
    assert result.structured_content["returned_hits"] == 1
    assert result.structured_content["hits"][0]["message"] == "service started"
    await client.aclose()


@pytest.mark.asyncio
async def test_search_logs_auto_discovers_service_field():
    captured = {}

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/sample-index/search":
            captured["body"] = request.read().decode()
        return quickwit_transport(request)

    settings = build_settings([])
    client = httpx.AsyncClient(base_url="http://quickwit.example", transport=httpx.MockTransport(transport))
    mcp = create_mcp(client, settings)

    assert mcp is not None
    async with Client(transport=mcp) as mcp_client:
        result = await mcp_client.call_tool(
            "search_logs",
            {
                "index_id": "sample-index",
                "subject": "Firehose",
                "subject_kind": "service",
                "start_time": "2026-05-31T14:00:00Z",
                "end_time": "2026-05-31T15:00:00Z",
            },
        )

    body = captured["body"]
    assert "service.name:Firehose" in body
    assert result.structured_content["query_plan"]["selected_fields"] == ["service.name"]
    assert result.structured_content["query_plan"]["subject_kind"] == "service"
    await client.aclose()


@pytest.mark.asyncio
async def test_search_logs_combines_text_and_subject():
    captured = {}

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/sample-index/search":
            captured["body"] = request.read().decode()
        return quickwit_transport(request)

    settings = build_settings([])
    client = httpx.AsyncClient(base_url="http://quickwit.example", transport=httpx.MockTransport(transport))
    mcp = create_mcp(client, settings)

    assert mcp is not None
    async with Client(transport=mcp) as mcp_client:
        await mcp_client.call_tool(
            "search_logs",
            {
                "index_id": "sample-index",
                "text": "error",
                "subject": "Firehose",
                "subject_kind": "service",
                "start_time": "2026-05-31T14:00:00Z",
                "end_time": "2026-05-31T15:00:00Z",
            },
        )

    assert '"query":"(error) AND (service.name:Firehose' in captured["body"]
    await client.aclose()


@pytest.mark.asyncio
async def test_search_logs_falls_back_to_free_text_when_no_service_field():
    captured = {}

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/indexes/sample-index":
            metadata = quickwit_transport(request).json()
            metadata["index_config"]["doc_mapping"]["field_mappings"] = [
                {"name": "timestamp", "type": "datetime"},
                {"name": "message", "type": "text"},
            ]
            return httpx.Response(200, json=metadata)
        if request.url.path == "/api/v1/sample-index/search":
            captured["body"] = request.read().decode()
        return quickwit_transport(request)

    settings = build_settings([])
    client = httpx.AsyncClient(base_url="http://quickwit.example", transport=httpx.MockTransport(transport))
    mcp = create_mcp(client, settings)

    assert mcp is not None
    async with Client(transport=mcp) as mcp_client:
        result = await mcp_client.call_tool(
            "search_logs",
            {
                "index_id": "sample-index",
                "subject": "Firehose",
                "subject_kind": "service",
                "start_time": "2026-05-31T14:00:00Z",
                "end_time": "2026-05-31T15:00:00Z",
            },
        )

    assert '"query":"(Firehose OR firehose)"' in captured["body"]
    assert result.structured_content["query_plan"]["selected_fields"] == []
    assert result.structured_content["query_plan"]["warnings"] == [
        "no service fields found; used free-text subject search"
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_search_logs_rejects_unknown_subject_kind():
    settings = build_settings([])
    client = httpx.AsyncClient(base_url="http://quickwit.example", transport=httpx.MockTransport(quickwit_transport))
    mcp = create_mcp(client, settings)

    assert mcp is not None
    async with Client(transport=mcp) as mcp_client:
        with pytest.raises(ToolError, match="subject_kind must be 'free_text' or 'service'"):
            await mcp_client.call_tool(
                "search_logs",
                {
                    "index_id": "sample-index",
                    "subject": "Firehose",
                    "subject_kind": "namespace",
                    "start_time": "2026-05-31T14:00:00Z",
                    "end_time": "2026-05-31T15:00:00Z",
                },
            )
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
        await mcp_client.call_tool("search", {"index_id": "sample-index", "query": "*", "max_hits": 1000})

    assert f'"max_hits":{MAX_SEARCH_HITS}' in captured["body"].replace(" ", "")
    await client.aclose()


@pytest.mark.asyncio
async def test_search_accepts_typed_advanced_params():
    captured = {}

    def transport(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={"num_hits": 0, "hits": [], "errors": []})

    settings = build_settings([])
    client = httpx.AsyncClient(base_url="http://quickwit.example", transport=httpx.MockTransport(transport))
    mcp = create_mcp(client, settings)

    assert mcp is not None
    async with Client(transport=mcp) as mcp_client:
        await mcp_client.call_tool(
            "search",
            {
                "index_id": "sample-index",
                "query": "error",
                "search_field": ["message"],
                "snippet_fields": ["message"],
                "sort_field": "timestamp",
                "sort_order": "desc",
                "aggs": {"statuses": {"terms": {"field": "status"}}},
            },
        )

    body = captured["body"].replace(" ", "")
    assert '"search_field":["message"]' in body
    assert '"snippet_fields":["message"]' in body
    assert '"sort_by":["-timestamp"]' in body
    assert '"aggs":{"statuses":{"terms":{"field":"status"}}}' in body
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
        await mcp_client.call_tool("list_splits", {"index_id": "sample-index", "limit": 1000})

    assert f"limit={MAX_SPLITS_LIMIT}" in captured["url"]
    await client.aclose()


@pytest.mark.parametrize("index_id", ["sample-index", "logs_2026", "tenant.prod.logs", "logs-01"])
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
