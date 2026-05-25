# Quickwit MCP Server

REST-based MCP server for [Quickwit](https://quickwit.io). It loads Quickwit's OpenAPI specification from `/openapi.json` and exposes REST endpoints as MCP tools.

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

The MCP server listens on `0.0.0.0:8080` by default.

## Configuration

Environment variables:

- `QUICKWIT_BASE_URL`: Quickwit REST base URL. Default: `http://localhost:7280`.
- `OPENAPI_SPEC_URL`: OpenAPI URL. Default: `${QUICKWIT_BASE_URL}/openapi.json`.
- `QUICKWIT_TOOL_MODE`: `full` or `readonly`. Default: `full`.
- `MCP_HOST`: MCP bind host. Default: `0.0.0.0`.
- `MCP_PORT`: MCP bind port. Default: `8080`.
- `MCP_ENDPOINT_PATH`: MCP endpoint path. Default: `/`.
- `MCP_USER_AGENT`: User-Agent used for Quickwit requests.

CLI flags:

```bash
python -m src.server \
  --quickwit-base-url "http://quickwit.example.vpn:7280" \
  --tool-mode full \
  --host 0.0.0.0 \
  --port 8080 \
  --endpoint-path /
```

## Tool modes

`full` is default and exposes all endpoints present in Quickwit's OpenAPI spec, including mutating operations.

Use readonly mode to remove mutating operations:

```bash
export QUICKWIT_TOOL_MODE=readonly
python -m src.server
```

Readonly mode keeps `GET` endpoints and POST search endpoints, while removing ingest, delete, clear, and index/source mutation endpoints.

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
