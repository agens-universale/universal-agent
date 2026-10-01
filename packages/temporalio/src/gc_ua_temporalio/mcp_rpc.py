"""Small authenticated loopback RPC to the host, separate from MCP transports."""

import asyncio
import json
import os


class McpHostError(RuntimeError):
    def __init__(self, kind, detail):
        super().__init__(detail)
        self.kind = kind


class McpHostClient:
    def __init__(self, config):
        self.port = config.mcp_host_port
        if not config.mcp_host_token_env or not os.environ.get(
            config.mcp_host_token_env
        ):
            raise ValueError("MCP host requires mcp_host_token_env")
        self.token = os.environ[config.mcp_host_token_env]

    async def request(self, method, **params):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.port, limit=1_048_576
        )
        try:
            writer.write(
                json.dumps(
                    {"token": self.token, "method": method, "params": params}
                ).encode()
                + b"\n"
            )
            await writer.drain()
            async with asyncio.timeout(45):
                raw = await reader.readline()
            if not raw:
                raise McpHostError(
                    "HOST_UNAVAILABLE", "Host closed the connection"
                )
            try:
                reply = json.loads(raw)
            except ValueError as exc:
                raise McpHostError(
                    "HOST_UNAVAILABLE", "Invalid host response"
                ) from exc
            if "error" in reply:
                raise McpHostError(
                    reply["error"], reply.get("detail", reply["error"])
                )
            return reply["result"]
        finally:
            writer.close()
            await writer.wait_closed()
