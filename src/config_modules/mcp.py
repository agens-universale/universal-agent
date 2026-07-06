from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class McpServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    transport: Literal["http", "sse", "stdio", "stream_sse", "stream_http", "stream_stdio"]
    url: str | None = None
    token: str | None = None
    server_name: str | None = Field(default=None, alias="server-name")
    description: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    persistent_session: bool = False
    persistent_session_close_tool: str | None = None
    # mTLS / TLS для http-транспортов: значения читает рантайм utils/ssl_config.py
    # (get_verify_ssl / get_client_cert) через config_dict["mcp-servers"].
    verify_ssl: bool | str = Field(default=True, alias="verify-ssl")
    client_cert: str | None = Field(default=None, alias="client-cert")
    client_key: str | None = Field(default=None, alias="client-key")

    @field_validator(
        "url", "token", "server_name", "description", "command", "cwd",
        "client_cert", "client_key", mode="before"
    )
    @classmethod
    def normalize_optional_text(cls, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("args", mode="before")
    @classmethod
    def normalize_args(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("mcp.servers.<name>.args должен быть списком строк")
        return [str(item) for item in value]

    @field_validator("env", mode="before")
    @classmethod
    def normalize_env(cls, value: object) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("mcp.servers.<name>.env должен быть объектом key/value")
        return {str(k): str(v) for k, v in value.items()}


class McpMetaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tools_cache_ttl: int = Field(default=3600, alias="tools-cache-ttl")
    request_timeout_sec: int = 600
    discovery_timeout_sec: int = 30
    recommended_mcp_manifest: str | None = Field(
        default=None, alias="recommended-mcp-manifest"
    )

    @field_validator(
        "tools_cache_ttl", "request_timeout_sec", "discovery_timeout_sec",
        mode="before",
    )
    @classmethod
    def validate_positive_int(cls, value: object) -> int:
        n = int(value)
        if n <= 0:
            raise ValueError("mcp.meta значения должны быть положительными числами")
        return n


class McpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    meta: McpMetaConfig = Field(default_factory=McpMetaConfig)
