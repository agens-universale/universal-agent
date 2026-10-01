"""MCP configuration stays on the resource host; workflows receive references."""

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class McpServerProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: str = "v1"
    transport: Literal[
        "stdio", "stream_stdio", "sse", "stream_sse", "http", "stream_http"
    ]
    persistent_session: bool = False
    execute_on: Literal["agent", "os_backend"] = "agent"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    token_env: str | None = None
    headers_env: dict[str, str] = Field(default_factory=dict)
    verify_ssl: bool | str = True
    client_cert: str | None = None
    client_key: str | None = None
    timeout_seconds: int = Field(default=120, ge=1, le=180)
    sampling: Literal[False] = False
    elicitation: Literal[False] = False

    @model_validator(mode="after")
    def endpoint(self):
        if self.transport in ("stdio", "stream_stdio"):
            if not self.command:
                raise ValueError("stdio MCP requires command")
        elif not self.url:
            raise ValueError("HTTP/SSE MCP requires url")
        return self

    def client_config(self, workspace):
        value = self.model_dump(
            exclude={
                "revision",
                "token_env",
                "headers_env",
                "timeout_seconds",
                "sampling",
                "elicitation",
            }
        )
        value["cwd"] = workspace
        value["verify-ssl"] = value.pop("verify_ssl")
        value["client-cert"] = value.pop("client_cert")
        value["client-key"] = value.pop("client_key")
        if self.token_env:
            value["token"] = os.environ[self.token_env]
        value["headers"] = {
            name: os.environ[env] for name, env in self.headers_env.items()
        }
        return value
