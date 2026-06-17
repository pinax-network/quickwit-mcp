# Quickwit MCP Server

REST-based MCP server for [Quickwit](https://quickwit.io). It exposes a curated set of readonly Quickwit tools for search and index inspection.

## Usage

Set the Quickwit server URL and start the MCP server:

```bash
export QUICKWIT_BASE_URL="http://quickwit.example.vpn:7280"
python -m src.server
```

Equivalent CLI flag:

```bash
python -m src.server --quickwit-base-url "http://quickwit.example.vpn:7280"
```

The MCP server listens on `127.0.0.1:8080` by default. Set `MCP_HOST=0.0.0.0` only when exposing it on a trusted network such as a VPN.

## Configuration

Environment variables:

- `QUICKWIT_BASE_URL`: Quickwit REST base URL. Default: `http://localhost:7280`.
- `MCP_HOST`: MCP bind host. Default: `127.0.0.1`.
- `MCP_PORT`: MCP bind port. Default: `8080`.
- `MCP_ENDPOINT_PATH`: MCP endpoint path. Default: `/`.
- `MCP_USER_AGENT`: User-Agent used for Quickwit requests.
- `MCP_DEFAULT_TIMEZONE`: Timezone used for naive RFC3339 inputs. Default: `UTC`.
- `MCP_MAX_SEARCH_WINDOW_SECONDS`: Maximum time range for log search tools. Default: `86400`.
- `MCP_RATE_LIMIT_RPS`: MCP request rate limit. Default: `5.0`.
- `MCP_RATE_LIMIT_BURST`: MCP request burst capacity. Default: `20`.
- `MCP_RESPONSE_LIMIT_BYTES`: Maximum MCP response size. Default: `500000`.

CLI flags:

```bash
python -m src.server \
  --quickwit-base-url "http://quickwit.example.vpn:7280" \
  --host 0.0.0.0 \
  --port 8080 \
  --endpoint-path /
```

## Tools

The server exposes only curated readonly tools: `search_logs`, `search`, `inspect_index`, `list_fields`, `describe_index`, `list_splits`, `list_indexes`, and `version`.

`list_indexes` returns both a stable summary and the raw Quickwit metadata for each index. Raw metadata is included because Quickwit metadata shapes vary across versions.

Search tools accept one exact index ID. Quickwit multi-target expressions such as `logs-*` and `a,b` are intentionally not exposed.

Prefer `search_logs` for bounded operational log search. It accepts RFC3339 `start_time` and `end_time`, sorts by the Quickwit `timestamp_field` by default when present, and returns compact truncated hits. You can pass `subject` with `subject_kind: "service"` to let the MCP server pick service-like fields from static index metadata before building the Quickwit query; responses include `query_plan` when this path is used.

Use `inspect_index` before searches to see Quickwit-native metadata: `timestamp_field`, `default_search_fields`, static field names, whether dynamic mapping is enabled, and raw index metadata. If `dynamic_mapping` is true or you need searchable/aggregatable field capabilities, call `list_fields` next.

Use `list_fields` to discover fields Quickwit has seen in indexed documents via the Elasticsearch-compatible `_field_caps` API. On large indexes, pass `start_timestamp` and `end_timestamp` in epoch seconds and optional `field_patterns` such as `["message", "resource_attributes.*"]` to keep discovery bounded.

Recommended agent workflow:

1. Call `list_indexes` to choose one exact index ID.
2. Call `inspect_index` to get static metadata and time/default fields.
3. Call `list_fields` when dynamic fields or field capabilities are needed.
4. Build a Quickwit query and call `search_logs` for RFC3339-bounded log searches, or `search` for lower-level Quickwit searches and aggregations.

Example log search arguments:

```json
{
  "index_id": "sample-index",
  "subject": "Firehose",
  "subject_kind": "service",
  "start_time": "2026-05-31T14:00:00Z",
  "end_time": "2026-05-31T15:00:00Z",
  "max_hits": 10
}
```

## Health endpoints

- `/health`: liveness check for the MCP process.
- `/ready`: readiness check that verifies Quickwit is reachable.

## Client proxy

For clients that need STDIO, run:

```bash
export MCP_SERVER_URL="http://localhost:8080/"
python -m src.client
```

## Docker

```bash
docker build -t quickwit-mcp .
docker run --rm -p 8080:8080 \
  -e QUICKWIT_BASE_URL="http://quickwit.example.vpn:7280" \
  quickwit-mcp
```

## Testing

```bash
pytest
```

Optional integration test against Quickwit:

```bash
export QUICKWIT_BASE_URL="http://quickwit.example.vpn:7280"
pytest -m integration
```
