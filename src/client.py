import os

from fastmcp import FastMCP

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8080/")

proxy = FastMCP.as_proxy(MCP_SERVER_URL, name="Quickwit MCP Proxy")

if __name__ == "__main__":
    proxy.run()
