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
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import PlainTextResponse

try:
    __version__ = version("quickwit-mcp")
except PackageNotFoundError:
    __version__ = "unknown"


DEFAULT_QUICKWIT_BASE_URL = "http://localhost:7280"
INDEX_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
FIELD_NAME_PATTERN = re.compile(r"^[A-Za-z_@][A-Za-z0-9_.@-]{0,254}$")
FIELD_PATTERN_PATTERN = re.compile(r"^[A-Za-z0-9_@.*-]{1,255}$")
MAX_SEARCH_HITS = 100
MAX_SPLITS_LIMIT = 100
MAX_TRUNCATE_FIELD_BYTES = 10_000

@dataclass(frozen=True)
class Settings:
    mcp_default_timezone: str
    mcp_endpoint_path: str
    mcp_host: str
    mcp_max_search_window_seconds: int
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
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "8080")))
    parser.add_argument("--endpoint-path", default=os.getenv("MCP_ENDPOINT_PATH", "/"))
    args = parser.parse_args(argv)

    quickwit_base_url = args.quickwit_base_url.rstrip("/")

    return Settings(
        mcp_default_timezone=_validate_timezone_name(os.getenv("MCP_DEFAULT_TIMEZONE", "UTC")),
        mcp_endpoint_path=args.endpoint_path,
        mcp_host=args.host,
        mcp_max_search_window_seconds=int(os.getenv("MCP_MAX_SEARCH_WINDOW_SECONDS", "86400")),
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

        @mcp.custom_route("/ready", methods=["GET"])
        async def _(_: Request) -> PlainTextResponse:
            try:
                await quickwit_request(client, "GET", "/api/v1/version")
            except ToolError as exc:
                return PlainTextResponse(f"Quickwit unavailable: {exc}", status_code=503)
            return PlainTextResponse("OK")

        @mcp.tool
        async def version() -> dict[str, Any]:
            """Return Quickwit node version and runtime information."""
            return await quickwit_request(client, "GET", "/api/v1/version")

        @mcp.tool
        async def list_indexes() -> dict[str, list[dict[str, Any]]]:
            """List Quickwit indexes with summary and raw metadata."""
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
        async def inspect_index(index_id: str) -> dict[str, Any]:
            """Inspect one exact Quickwit index before searching.

            Use this first to identify the timestamp field, default search fields, and static index mapping.
            For dynamic fields discovered from indexed documents, call list_fields with a bounded time range.
            """
            metadata = await _get_index_metadata(client, index_id)
            return _inspect_index_metadata(metadata)

        @mcp.tool
        async def list_fields(
            index_id: str,
            field_patterns: list[str] | None = None,
            start_timestamp: int | None = None,
            end_timestamp: int | None = None,
        ) -> dict[str, Any]:
            """List searchable/aggregatable fields discovered by Quickwit for one index.

            Use after inspect_index when an index uses dynamic mapping or when the query needs fields that are
            present in documents but not listed in static metadata. Provide start_timestamp/end_timestamp in epoch
            seconds to limit discovery cost on large indexes. field_patterns accepts Quickwit field globs such as
            ["message", "resource_attributes.*"].
            """
            _validate_index_id(index_id)
            field_patterns = _validate_field_patterns(field_patterns)
            start_timestamp = _validate_optional_int(start_timestamp, "start_timestamp")
            end_timestamp = _validate_optional_int(end_timestamp, "end_timestamp")
            params = _strip_none({
                "fields": _join_simple_list(field_patterns),
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
            })
            return await quickwit_request(client, "GET", f"/api/v1/_elastic/{index_id}/_field_caps", params=params)

        @mcp.tool
        async def search_logs(
            index_id: str,
            start_time: str,
            end_time: str,
            text: str = "*",
            subject: str | None = None,
            subject_kind: str = "free_text",
            auto_discover_fields: bool = True,
            fields: list[str] | None = None,
            sort_field: str | None = None,
            sort_order: str = "desc",
            max_hits: int = 20,
            truncate_field_bytes: int = 2000,
            include_raw: bool = False,
        ) -> dict[str, Any]:
            """Preferred log search: use RFC3339 time bounds and return compact operational log results."""
            metadata = await _get_index_metadata(client, index_id)
            inspection = _inspect_index_metadata(metadata)
            start_timestamp, end_timestamp, warnings = _parse_time_window(start_time, end_time, settings)
            timestamp_field = sort_field or _metadata_field_name(inspection, "timestamp_field")
            query, query_plan = _build_auto_query(
                base_query=_clean_query(text),
                subject=subject,
                subject_kind=subject_kind,
                field_names=set(inspection["field_names"]),
                auto_discover_fields=auto_discover_fields,
            )
            body = _build_log_search_body(
                query=query,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                max_hits=max_hits,
                sort_field=timestamp_field,
                sort_order=sort_order,
            )
            result = await quickwit_request(client, "POST", f"/api/v1/{index_id}/search", json_body=body)
            return _compact_search_response(
                result,
                fields=fields,
                truncate_field_bytes=truncate_field_bytes,
                include_raw=include_raw,
                warnings=warnings,
                query_plan=query_plan,
            )

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
            sort_field: str | None = None,
            sort_order: str = "desc",
            aggs: dict[str, Any] | None = None,
            count_all: bool = False,
            allow_failed_splits: bool = False,
        ) -> dict[str, Any]:
            """Low-level Quickwit search for advanced use. Prefer search_logs for operational logs."""
            _validate_index_id(index_id)
            if not isinstance(query, str):
                raise ToolError("query must be a string")
            query = query.strip()
            if not query:
                raise ToolError("query must not be empty")
            max_hits = _clamp_int(max_hits, minimum=0, maximum=MAX_SEARCH_HITS, name="max_hits")
            start_offset = _clamp_int(start_offset, minimum=0, maximum=1_000_000, name="start_offset")
            search_field = _validate_string_list(search_field, "search_field")
            snippet_fields = _validate_string_list(snippet_fields, "snippet_fields")
            sort_by = _build_sort_by(sort_field, sort_order)
            start_timestamp = _validate_optional_int(start_timestamp, "start_timestamp")
            end_timestamp = _validate_optional_int(end_timestamp, "end_timestamp")
            if aggs is not None and not isinstance(aggs, dict):
                raise ToolError("aggs must be an object")
            body = _strip_none({
                "query": query,
                "max_hits": max_hits,
                "start_offset": start_offset,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "search_field": _join_simple_list(search_field),
                "snippet_fields": _join_simple_list(snippet_fields),
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
            split_states = _validate_string_list(split_states, "split_states")
            start_timestamp = _validate_optional_int(start_timestamp, "start_timestamp")
            end_timestamp = _validate_optional_int(end_timestamp, "end_timestamp")
            end_create_timestamp = _validate_optional_int(end_create_timestamp, "end_create_timestamp")
            params = {
                "offset": offset,
                "limit": limit,
                "split_states": split_states,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "end_create_timestamp": end_create_timestamp,
            }
            return await quickwit_request(client, "GET", f"/api/v1/indexes/{index_id}/splits", params=params)

        _add_optional_middleware(mcp, settings)
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
    await _log_quickwit_compatibility(HTTP_CLIENT)

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


async def _log_quickwit_compatibility(client: httpx.AsyncClient) -> None:
    try:
        version_info = await quickwit_request(client, "GET", "/api/v1/version")
    except ToolError as exc:
        logger.warning("Quickwit compatibility check failed: %s", exc)
        return

    quickwit_version = None
    if isinstance(version_info, dict):
        build = version_info.get("build")
        if isinstance(build, dict):
            quickwit_version = build.get("version") or build.get("cargo_pkg_version")
    logger.info("Quickwit version: %s", quickwit_version or "unknown")


async def _get_index_metadata(client: httpx.AsyncClient, index_id: str) -> dict[str, Any]:
    _validate_index_id(index_id)
    metadata = await quickwit_request(client, "GET", f"/api/v1/indexes/{index_id}")
    if not isinstance(metadata, dict):
        raise ToolError("Quickwit returned unexpected index metadata")
    return metadata


def _inspect_index_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    summary = _simplify_index_metadata(metadata)["summary"]
    field_names = _extract_field_names(metadata)
    dynamic_mapping = _has_dynamic_mapping(metadata)
    return {
        "index_id": summary["index_id"],
        "index_uri": summary["index_uri"],
        "version": summary["version"],
        "timestamp_field": summary["timestamp_field"],
        "default_search_fields": summary["default_search_fields"],
        "field_names": sorted(field_names),
        "dynamic_mapping": dynamic_mapping,
        "field_discovery_hint": (
            "This index has dynamic mapping; field_names only contains static metadata fields. "
            "Call list_fields with a bounded time range to discover fields present in indexed documents."
            if dynamic_mapping
            else "field_names comes from static index metadata. Call list_fields if you need searchable/aggregatable capabilities."
        ),
        "raw_metadata": metadata,
    }


def _parse_time_window(start_time: str, end_time: str, settings: Settings) -> tuple[int, int, list[str]]:
    default_timezone = ZoneInfo(settings.mcp_default_timezone)
    start_timestamp, start_warnings = _parse_time(start_time, default_timezone, "start_time")
    end_timestamp, end_warnings = _parse_time(end_time, default_timezone, "end_time")
    if start_timestamp >= end_timestamp:
        raise ToolError("start_time must be before end_time")
    window_seconds = end_timestamp - start_timestamp
    if window_seconds > settings.mcp_max_search_window_seconds:
        raise ToolError(f"time window must be <= {settings.mcp_max_search_window_seconds} seconds")
    return start_timestamp, end_timestamp, [*start_warnings, *end_warnings]


def _parse_time(value: str, default_timezone: ZoneInfo, name: str) -> tuple[int, list[str]]:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{name} must be an RFC3339 datetime string")
    raw_value = value.strip()
    normalized = raw_value[:-1] + "+00:00" if raw_value.endswith("Z") else raw_value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ToolError(f"{name} must be an RFC3339 datetime string") from exc

    warnings = []
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_timezone)
        warnings.append(f"{name} had no timezone; interpreted as {default_timezone.key}")
    return int(parsed.timestamp()), warnings


def _build_log_search_body(
    *,
    query: str,
    start_timestamp: int,
    end_timestamp: int,
    max_hits: int,
    sort_field: str | None,
    sort_order: str,
) -> dict[str, Any]:
    max_hits = _clamp_int(max_hits, minimum=1, maximum=MAX_SEARCH_HITS, name="max_hits")
    return _strip_none({
        "query": query,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "max_hits": max_hits,
        "sort_by": _build_sort_by(sort_field, sort_order),
    }) or {}


def _build_auto_query(
    *,
    base_query: str,
    subject: str | None,
    subject_kind: str,
    field_names: set[str],
    auto_discover_fields: bool,
) -> tuple[str, dict[str, Any] | None]:
    if subject is None or not str(subject).strip():
        return base_query, None
    if not isinstance(subject, str):
        raise ToolError("subject must be a string")
    if not isinstance(auto_discover_fields, bool):
        raise ToolError("auto_discover_fields must be a boolean")

    subject = subject.strip()
    if len(subject) > 256:
        raise ToolError("subject must be <= 256 characters")
    if subject_kind not in {"free_text", "service"}:
        raise ToolError("subject_kind must be 'free_text' or 'service'")

    warnings = []
    selected_fields: list[str] = []
    if auto_discover_fields:
        selected_fields = _select_subject_fields(field_names, subject_kind)
        if subject_kind != "free_text" and not selected_fields:
            warnings.append(f"no {subject_kind} fields found; used free-text subject search")

    subject_clause = _format_subject_clause(subject, selected_fields)
    query = _combine_queries(base_query, subject_clause)
    return query, {
        "base_query": base_query,
        "subject": subject,
        "subject_kind": subject_kind,
        "auto_discover_fields": auto_discover_fields,
        "selected_fields": selected_fields,
        "query": query,
        "warnings": warnings,
    }


def _select_subject_fields(field_names: set[str], subject_kind: str) -> list[str]:
    if subject_kind != "service":
        return []
    scored = sorted(
        ((field, _score_service_field(field)) for field in field_names if FIELD_NAME_PATTERN.fullmatch(field)),
        key=lambda item: (-item[1], item[0]),
    )
    return [field for field, score in scored if score > 0][:5]


def _score_service_field(field_name: str) -> int:
    normalized = field_name.lower().replace("_", ".")
    exact_scores = {
        "service.name": 100,
        "service": 95,
        "app": 85,
        "application": 85,
        "component": 75,
        "k8s.container.name": 60,
        "kubernetes.container.name": 60,
        "k8s.deployment.name": 50,
        "k8s.pod.name": 50,
    }
    if normalized in exact_scores:
        return exact_scores[normalized]
    if "service" in normalized:
        return 70
    return 0


def _format_subject_clause(subject: str, fields: list[str]) -> str:
    values = [subject]
    lowered = subject.lower()
    if lowered != subject:
        values.append(lowered)

    terms = [_format_query_value(value) for value in values]
    if fields:
        clauses = [f"{field}:{term}" for field in fields for term in terms]
    else:
        clauses = terms
    if len(clauses) == 1:
        return clauses[0]
    return "(" + " OR ".join(clauses) + ")"


def _format_query_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _combine_queries(base_query: str, subject_clause: str) -> str:
    if base_query == "*":
        return subject_clause
    return f"({base_query}) AND {subject_clause}"


def _build_sort_by(sort_field: str | None, sort_order: str) -> str | None:
    if sort_field is None:
        return None
    _validate_field_name(sort_field, "sort_field")
    if sort_order not in {"asc", "desc"}:
        raise ToolError("sort_order must be 'asc' or 'desc'")
    return f"-{sort_field}" if sort_order == "asc" else sort_field


def _join_simple_list(values: list[str] | None) -> str | None:
    if not values:
        return None
    return ",".join(values)


def _compact_search_response(
    result: Any,
    *,
    fields: list[str] | None,
    truncate_field_bytes: int,
    include_raw: bool,
    warnings: list[str],
    query_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ToolError("Quickwit returned unexpected search response")
    hits = result.get("hits")
    if not isinstance(hits, list):
        raise ToolError("Quickwit returned unexpected search hits")
    selected_fields = _validate_field_list(fields, "fields") or []
    truncate_field_bytes = _clamp_int(
        truncate_field_bytes,
        minimum=0,
        maximum=MAX_TRUNCATE_FIELD_BYTES,
        name="truncate_field_bytes",
    )

    compact_hits = []
    truncated = False
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        compact_hit, hit_truncated = _compact_hit(hit, selected_fields, truncate_field_bytes)
        compact_hits.append(compact_hit)
        truncated = truncated or hit_truncated

    response = {
        "num_hits": result.get("num_hits"),
        "returned_hits": len(compact_hits),
        "elapsed_time_micros": result.get("elapsed_time_micros"),
        "hits": compact_hits,
        "truncated": truncated,
        "warnings": warnings,
    }
    if include_raw:
        response["raw_response"] = result
    if query_plan is not None:
        response["query_plan"] = query_plan
    return response


def _compact_hit(hit: dict[str, Any], fields: list[str], truncate_field_bytes: int) -> tuple[dict[str, Any], bool]:
    compact_hit = {}
    truncated = False
    for field in fields:
        value = _get_nested_value(hit, field)
        if value is None:
            continue
        if isinstance(value, str):
            value, was_truncated = _truncate_string(value, truncate_field_bytes)
            truncated = truncated or was_truncated
        compact_hit[field] = value
    if compact_hit:
        return compact_hit, truncated
    fallback = dict(hit)
    for key, value in list(fallback.items()):
        if isinstance(value, str):
            fallback[key], was_truncated = _truncate_string(value, truncate_field_bytes)
            truncated = truncated or was_truncated
    return fallback, truncated


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


def _validate_timezone_name(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"invalid MCP_DEFAULT_TIMEZONE: {value}") from exc
    return value


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
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolError(f"{name} must be an integer")
    if value < minimum:
        raise ToolError(f"{name} must be >= {minimum}")
    return min(value, maximum)


def _validate_optional_int(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolError(f"{name} must be an integer")
    return value


def _validate_string_list(value: list[str] | None, name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ToolError(f"{name} must be a list of strings")
    cleaned = []
    for item in value:
        if not isinstance(item, str):
            raise ToolError(f"{name} must be a list of strings")
        item = item.strip()
        if not item:
            raise ToolError(f"{name} must not contain empty strings")
        cleaned.append(item)
    return cleaned


def _validate_field_list(value: list[str] | None, name: str) -> list[str] | None:
    cleaned = _validate_string_list(value, name)
    if cleaned is None:
        return None
    for item in cleaned:
        _validate_field_name(item, name)
    return cleaned


def _validate_field_patterns(value: list[str] | None) -> list[str] | None:
    cleaned = _validate_string_list(value, "field_patterns")
    if cleaned is None:
        return None
    for item in cleaned:
        if not FIELD_PATTERN_PATTERN.fullmatch(item):
            raise ToolError("field_patterns contains an invalid pattern")
    return cleaned


def _validate_field_name(value: str, name: str) -> None:
    if not isinstance(value, str) or not FIELD_NAME_PATTERN.fullmatch(value):
        raise ToolError(f"{name} is invalid")


def _validate_index_id(index_id: str) -> None:
    if not isinstance(index_id, str):
        raise ToolError("index_id is invalid")
    if not INDEX_ID_PATTERN.fullmatch(index_id):
        raise ToolError("index_id is invalid")


def _clean_query(value: str | None) -> str:
    if value is None:
        return "*"
    if not isinstance(value, str):
        raise ToolError("text must be a string")
    value = value.strip()
    return value or "*"


def _simplify_index_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    index_config = metadata.get("index_config") if isinstance(metadata.get("index_config"), dict) else metadata
    doc_mapping = index_config.get("doc_mapping") if isinstance(index_config.get("doc_mapping"), dict) else {}
    search_settings = index_config.get("search_settings") if isinstance(index_config.get("search_settings"), dict) else {}
    return {
        "summary": {
            "index_id": _extract_index_id(metadata, index_config),
            "index_uri": _first_string(index_config.get("index_uri"), metadata.get("index_uri")),
            "version": _first_string(index_config.get("version"), metadata.get("version")),
            "timestamp_field": _first_string(doc_mapping.get("timestamp_field"), metadata.get("timestamp_field")),
            "default_search_fields": _extract_default_search_fields(search_settings),
        },
        "raw_metadata": metadata,
    }


def _extract_field_names(metadata: dict[str, Any]) -> set[str]:
    index_config = metadata.get("index_config") if isinstance(metadata.get("index_config"), dict) else metadata
    doc_mapping = index_config.get("doc_mapping") if isinstance(index_config.get("doc_mapping"), dict) else {}
    field_mappings = doc_mapping.get("field_mappings")
    field_names: set[str] = set()
    if isinstance(field_mappings, list):
        for item in field_mappings:
            _collect_field_names(item, field_names)
    timestamp_field = doc_mapping.get("timestamp_field")
    if isinstance(timestamp_field, str):
        field_names.add(timestamp_field)
    search_settings = index_config.get("search_settings") if isinstance(index_config.get("search_settings"), dict) else {}
    for field in _extract_default_search_fields(search_settings):
        field_names.add(field)
    return field_names


def _has_dynamic_mapping(metadata: dict[str, Any]) -> bool:
    index_config = metadata.get("index_config") if isinstance(metadata.get("index_config"), dict) else metadata
    doc_mapping = index_config.get("doc_mapping") if isinstance(index_config.get("doc_mapping"), dict) else {}
    return isinstance(doc_mapping.get("dynamic_mapping"), dict)


def _collect_field_names(mapping: Any, field_names: set[str], prefix: str = "") -> None:
    if not isinstance(mapping, dict):
        return
    name = mapping.get("name")
    current_prefix = prefix
    if isinstance(name, str) and name:
        full_name = f"{prefix}.{name}" if prefix else name
        field_names.add(full_name)
        current_prefix = full_name
    nested = mapping.get("field_mappings") or mapping.get("fields")
    if isinstance(nested, list):
        for item in nested:
            _collect_field_names(item, field_names, current_prefix)


def _metadata_field_name(inspection: dict[str, Any], name: str) -> str | None:
    value = inspection.get(name)
    return value if isinstance(value, str) else None


def _get_nested_value(data: dict[str, Any], field: str) -> Any:
    if field in data:
        return data[field]
    for wrapper in ("_source", "doc"):
        wrapped = data.get(wrapper)
        if isinstance(wrapped, dict):
            wrapped_value = _get_nested_value(wrapped, field)
            if wrapped_value is not None:
                return wrapped_value
    current: Any = data
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _truncate_string(value: str, max_bytes: int) -> tuple[str, bool]:
    if max_bytes == 0:
        return "", bool(value)
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return f"{truncated}…", True


def _extract_index_id(metadata: dict[str, Any], index_config: dict[str, Any]) -> str | None:
    direct_value = _first_string(index_config.get("index_id"), metadata.get("index_id"))
    if direct_value:
        return direct_value

    index_uid = _first_string(index_config.get("index_uid"), metadata.get("index_uid"))
    if index_uid:
        return index_uid.split(":", maxsplit=1)[0]
    return None


def _extract_default_search_fields(search_settings: dict[str, Any]) -> list[str]:
    value = search_settings.get("default_search_fields", [])
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


if __name__ == "__main__":
    asyncio.run(main())
