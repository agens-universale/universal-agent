from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

ALLOWED_COMMAND_MODES = {"allow", "ask", "deny"}
ALLOWED_BINARY_READ_MODES = {"allow", "ask", "deny"}

_DEFAULT_BINARY_ALLOWED_TYPES = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/png",
    "image/jpeg",
]

_DEFAULT_BINARY_ALLOWED_EXTENSIONS = [
    ".pdf",
    ".docx",
    ".png",
    ".jpg",
    ".jpeg",
]


def validate_command_moderation_mode(raw_mode: Any) -> str:
    mode = str(raw_mode if raw_mode is not None else "deny").strip().lower()
    if mode not in ALLOWED_COMMAND_MODES:
        raise ValueError(
            "command_moderation.mode должен быть 'allow', 'ask' или 'deny'"
        )
    return mode


def validate_read_guard_mode(raw_mode: Any) -> str:
    mode = str(raw_mode if raw_mode is not None else "deny").strip().lower()
    if mode not in ALLOWED_COMMAND_MODES:
        raise ValueError(
            "read_guard.mode должен быть 'allow', 'ask' или 'deny'"
        )
    return mode


def validate_binary_read_policy(raw_mode: Any) -> str:
    mode = str(raw_mode if raw_mode is not None else "ask").strip().lower()
    if mode not in ALLOWED_BINARY_READ_MODES:
        raise ValueError(
            "read_guard.binary_read_policy должен быть 'allow', 'ask' или 'deny'"
        )
    return mode


class WriteGuardConfig(BaseModel):
    """
    Write-периметр (бывший root_box). On/ask по дефолту (secure-by-default).

    mode:
      deny  — нарушение → блок с `write_guard_violation`;
      ask   — нарушение → запрос подтверждения у пользователя (interactive);
      allow — нарушение → пропустить, audit log пометит `out_of_perimeter`.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    mode: str = "ask"
    extra_paths: list[str] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        mode = str(value or "deny").strip().lower()
        if mode not in {"allow", "ask", "deny"}:
            raise ValueError(
                "security.write_guard.mode должен быть 'allow', 'ask' или 'deny'"
            )
        return mode


class UrlPolicyConfig(BaseModel):
    """
    URL-policy для fetch_url/simple_fetch_url. On/ask по дефолту (secure-by-default).

    mode:
      deny  — нарушение → блок с `url_blocked_by_policy`;
      ask   — нарушение → запрос подтверждения у пользователя (interactive);
      allow — нарушение → пропустить, audit log пометит `out_of_perimeter`.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    mode: str = "ask"
    allowed_schemes: list[str] = Field(
        default_factory=lambda: ["http", "https"]
    )
    url_blacklist: list[str] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        mode = str(value or "deny").strip().lower()
        if mode not in {"allow", "ask", "deny"}:
            raise ValueError(
                "security.url_policy.mode должен быть 'allow', 'ask' или 'deny'"
            )
        return mode


class ReadGuardConfig(BaseModel):
    """
    Контур чтения read_guard. On/ask по дефолту (secure-by-default).

    В v1 реализован только Контур 1 (детерминированный path-policy).
    Поля для бинарных файлов и size‑лимита присутствуют в схеме для
    forward‑compat, но в v1 не enforce'ятся (kept-for-future).
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    mode: str = "ask"
    # Пустой список трактуется как [launch_root] — строгий периметр проекта.
    # Спецэлемент "*" / "all" включает allow-by-default (см. spec).
    allowed_read_roots: list[str] = Field(default_factory=list)
    # Исключения из denied_*: переопределяют denylist для конкретных путей.
    allowed_read_paths: list[str] = Field(default_factory=list)
    denied_read_paths: list[str] = Field(default_factory=list)
    denied_file_patterns: list[str] = Field(default_factory=list)
    denied_extensions: list[str] = Field(default_factory=list)
    # forward-compat: размер и бинарная политика (не enforce'ятся в v1).
    max_file_size_kb: int = 1024
    binary_read_policy: str = "ask"
    binary_allowed_types: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_BINARY_ALLOWED_TYPES)
    )
    binary_allowed_extensions: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_BINARY_ALLOWED_EXTENSIONS)
    )

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        try:
            return validate_read_guard_mode(value)
        except ValueError as exc:
            raise ValueError(
                "security.read_guard.mode должен быть 'allow', 'ask' или 'deny'"
            ) from exc

    @field_validator("binary_read_policy")
    @classmethod
    def validate_binary(cls, value: str) -> str:
        return validate_binary_read_policy(value)


class DownloadConfig(BaseModel):
    """Настройки тула download_file: каталог загрузки и лимит размера тела ответа."""

    model_config = ConfigDict(extra="forbid")
    download_dir: str = "downloads"
    max_bytes: int = 52_428_800


class CommandModerationConfig(BaseModel):
    """
    Judge-настройки для execute_command/run_python_code. On/ask по дефолту (secure-by-default).

    enabled=False — слой выключен полностью, judge не вызывается (всё проходит).
    enabled=True + mode=allow — то же самое (без судьи).
    enabled=True + mode=ask/deny — судья активен.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    mode: str = "ask"
    allowed_command_examples: list[str] = Field(default_factory=list)
    denied_command_examples: list[str] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        try:
            return validate_command_moderation_mode(value)
        except ValueError as exc:
            raise ValueError(
                "security.command_moderation.mode должен быть 'allow', 'ask' или 'deny'"
            ) from exc


class ProtectedPathsConfig(BaseModel):
    """
    Floor-защита системных путей (enabled по умолчанию).
    extra — дополнительные пути, которые нельзя трогать.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    extra: list[str] = Field(default_factory=list)


class SecurityConfig(BaseModel):
    """
    Независимые слои безопасности. Все on/ask по дефолту (secure-by-default).
    Структура (clean break c прежней вложенностью под command_moderation):
        security:
          command_moderation: {mode, allowed_command_examples}
          write_guard:        {enabled, extra_paths}
          read_guard:         {enabled, mode, denied_*, allowed_*}
          url_policy:         {enabled, allowed_schemes, url_blacklist}
          download:           {download_dir, max_bytes}
          protected_paths:    {enabled, extra}
    """

    model_config = ConfigDict(extra="forbid")
    command_moderation: CommandModerationConfig = Field(
        default_factory=CommandModerationConfig
    )
    write_guard: WriteGuardConfig = Field(default_factory=WriteGuardConfig)
    read_guard: ReadGuardConfig = Field(default_factory=ReadGuardConfig)
    url_policy: UrlPolicyConfig = Field(default_factory=UrlPolicyConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    protected_paths: ProtectedPathsConfig = Field(
        default_factory=ProtectedPathsConfig
    )


def apply_cli_command_moderation_overrides(
    config_dict: Dict[str, Any],
    *,
    write_guard_enabled: Optional[bool] = None,
    command_moderation_mode: Optional[str] = None,
    read_guard_enabled: Optional[bool] = None,
    read_guard_mode: Optional[str] = None,
) -> None:
    """
    Накладывает CLI-override'ы на top-level config_dict (после load_config).
    Поля верхнего уровня: command_moderation, write_guard, read_guard, url_policy.
    """
    command_moderation = config_dict.setdefault("command_moderation", {})
    if command_moderation_mode is not None:
        command_moderation["mode"] = validate_command_moderation_mode(
            command_moderation_mode
        )
    elif "mode" not in command_moderation:
        command_moderation["mode"] = "ask"
    command_moderation.setdefault("enabled", True)

    write_guard = config_dict.setdefault("write_guard", {})
    if write_guard_enabled is not None:
        write_guard["enabled"] = bool(write_guard_enabled)
    elif "enabled" not in write_guard:
        write_guard["enabled"] = True

    read_guard = config_dict.setdefault("read_guard", {})
    if read_guard_enabled is not None:
        read_guard["enabled"] = bool(read_guard_enabled)
    elif "enabled" not in read_guard:
        read_guard["enabled"] = True
    if read_guard_mode is not None:
        read_guard["mode"] = validate_read_guard_mode(read_guard_mode)
    elif "mode" not in read_guard:
        read_guard["mode"] = "ask"
