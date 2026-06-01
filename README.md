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

The server exposes only curated readonly tools: `search`, `describe_index`, `list_splits`, `list_indexes`, and `version`.

`list_indexes` returns both a stable summary and the raw Quickwit metadata for each index. Raw metadata is included because Quickwit metadata shapes vary across versions.

`search` accepts one exact index ID. Quickwit multi-target expressions such as `logs-*` and `a,b` are intentionally not exposed.

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
