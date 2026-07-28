from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class McpToolsWhitelistConfig(BaseModel):
    """Whitelist тулов сервера: enabled=True → модель видит строго `tools`."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    tools: list[str] = Field(default_factory=list)

    @field_validator("tools", mode="before")
    @classmethod
    def normalize_tools(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(
                "mcp.servers.<name>.whitelist.tools должен быть списком строк"
            )
        seen: set[str] = set()
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
        return out


class McpServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    transport: Literal["http", "sse", "stdio", "stream_sse", "stream_http", "stream_stdio"]
    url: str | None = None
    token: str | None = None
    # Дополнительные HTTP-headers для MCP HTTP/SSE транспортов.
    # Применяются для транспортов: http, sse, stream_http, stream_sse.
    # Если задан `headers.Authorization`, он переопределяет Authorization из `token`.
    headers: dict[str, str] = Field(default_factory=dict)
    server_name: str | None = Field(default=None, alias="server-name")
    description: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    persistent_session: bool = False
    persistent_session_close_tool: str | None = None
    # Whitelist тулов сервера. Нет блока / enabled=False → все тулы доступны;
    # enabled=True → строго tools (пустой список = ноль тулов). Имена базовые,
    # как в tools/list, без префикса mcp_<server>_.
    whitelist: McpToolsWhitelistConfig | None = None
    # Отправлять ли внешний trace_id этому серверу. False (дефолт) — не
    # отправлять ничего: внутренний корреляционный id не уходит наружу, пока
    # его не разрешили явно (в mcp-servers могут быть сторонние эндпойнты).
    # True — header для http/sse/stream_http/stream_sse и env TRACE_ID для
    # stdio/stream_stdio.
    trace_header: bool = False
    # mTLS / TLS для http-транспортов: значения читает рантайм utils/ssl_config.py
    # (get_verify_ssl / get_client_cert) через config_dict["mcp-servers"].
    # Унифицировано с backend: на входе принимаются use-ssl / use_ssl / verify-ssl,
    # в config_dict дампится как verify-ssl (его читает get_verify_ssl).
    verify_ssl: bool | str = Field(
        default=True,
        validation_alias=AliasChoices("verify_ssl", "verify-ssl", "use_ssl", "use-ssl"),
        serialization_alias="verify-ssl",
    )
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

    @field_validator("headers", mode="before")
    @classmethod
    def normalize_headers(cls, value: object) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("mcp.servers.<name>.headers должен быть объектом key/value")
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
