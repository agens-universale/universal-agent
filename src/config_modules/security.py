from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

ALLOWED_COMMAND_MODES = {"allow", "ask", "deny"}
ALLOWED_BINARY_READ_MODES = {"allow", "ask", "deny"}

# Built-in denylist of read paths. Поддерживаются плейсхолдеры:
#   ~              → home директория текущего пользователя;
#   %USERPROFILE%  → переменная окружения USERPROFILE (Windows);
#   <launch_root>  → корень запуска (launch_root из ToolSecurityContext).
_DEFAULT_DENIED_READ_PATHS = [
    "~/.ssh",
    "~/.aws",
    "~/.config/gcloud",
    "~/.gnupg",
    "/etc/ssl/private",
    "/proc",
    "/sys",
    "/dev",
    # gc-state (см. read-guard-spec.md «Защита gc/ директорий»)
    "~/.local/state/gc",
    "%USERPROFILE%/.gc",
    "<launch_root>/.gc",
]

# Исключения из denylist (явно разрешены, даже если совпадают с denied_*).
# Нужно для skills/ внутри .gc/ директорий — они public API расширения.
_DEFAULT_ALLOWED_READ_PATHS = [
    "~/.local/state/gc/skills",
    "%USERPROFILE%/.gc/skills",
    "<launch_root>/.gc/skills",
]

_DEFAULT_DENIED_FILE_PATTERNS = [
    ".env",
    ".env.*",
    "*id_rsa*",
    "*id_ed25519*",
    "*.pem",
    "*.p12",
    "*.kdbx",
    "*credentials*.json",
    "*.sqlite",
    "*.db",
]

_DEFAULT_DENIED_EXTENSIONS = [
    ".key",
    ".pfx",
    ".jks",
    ".asc",
]

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

# Дефолтный blacklist для url_policy — закрывает базовый SSRF.
# Включает:
#   - cloud metadata endpoints (AWS, GCP, Azure);
#   - localhost / loopback;
#   - RFC1918 private networks.
# Поддерживаются точные хосты и fnmatch wildcards (см. url_policy.match_url_blacklist).
_DEFAULT_URL_BLACKLIST = [
    # Cloud metadata endpoints
    "169.254.169.254",
    "metadata.google.internal",
    "*.metadata.google.internal",
    "metadata.azure.com",
    "*.metadata.azure.com",
    # Loopback / link-local
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "169.254.*",
    # RFC1918 private networks (полный 10/8, 172.16/12, 192.168/16)
    "10.*",
    "172.16.*", "172.17.*", "172.18.*", "172.19.*",
    "172.20.*", "172.21.*", "172.22.*", "172.23.*",
    "172.24.*", "172.25.*", "172.26.*", "172.27.*",
    "172.28.*", "172.29.*", "172.30.*", "172.31.*",
    "192.168.*",
]


def validate_command_moderation_mode(raw_mode: Any) -> str:
    mode = str(raw_mode if raw_mode is not None else "deny").strip().lower()
    if mode not in ALLOWED_COMMAND_MODES:
        raise ValueError("command_moderation.mode должен быть 'allow', 'ask' или 'deny'")
    return mode


def validate_read_guard_mode(raw_mode: Any) -> str:
    mode = str(raw_mode if raw_mode is not None else "deny").strip().lower()
    if mode not in ALLOWED_COMMAND_MODES:
        raise ValueError("read_guard.mode должен быть 'allow', 'ask' или 'deny'")
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
    Write-периметр (бывший root_box). Safe-by-default-OFF: enabled=False.

    mode:
      deny  — нарушение → блок с `write_guard_violation` (по умолчанию);
      ask   — нарушение → запрос подтверждения у пользователя (interactive);
      allow — нарушение → пропустить, audit log пометит `out_of_perimeter`.
    """
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    mode: str = "deny"
    extra_paths: list[str] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        mode = str(value or "deny").strip().lower()
        if mode not in {"allow", "ask", "deny"}:
            raise ValueError("security.write_guard.mode должен быть 'allow', 'ask' или 'deny'")
        return mode


class UrlPolicyConfig(BaseModel):
    """URL-policy для fetch_url/simple_fetch_url. Safe-by-default-OFF: enabled=False."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    allowed_schemes: list[str] = Field(default_factory=lambda: ["http", "https"])
    url_blacklist: list[str] = Field(default_factory=list)


class ReadGuardConfig(BaseModel):
    """
    Контур чтения read_guard. Safe-by-default-OFF: enabled=False.

    В v1 реализован только Контур 1 (детерминированный path-policy).
    Поля для бинарных файлов и size‑лимита присутствуют в схеме для
    forward‑compat, но в v1 не enforce'ятся (kept-for-future).
    """
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    mode: str = "deny"
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
    binary_allowed_types: list[str] = Field(default_factory=lambda: list(_DEFAULT_BINARY_ALLOWED_TYPES))
    binary_allowed_extensions: list[str] = Field(default_factory=lambda: list(_DEFAULT_BINARY_ALLOWED_EXTENSIONS))

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


class CommandModerationConfig(BaseModel):
    """
    Judge-настройки для execute_command/run_python_code. Safe-by-default-OFF.

    enabled=False — слой выключен полностью, judge не вызывается (всё проходит).
    enabled=True + mode=allow — то же самое (без судьи).
    enabled=True + mode=ask/deny — судья активен.
    """
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    mode: str = "allow"
    allowed_command_examples: list[str] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        try:
            return validate_command_moderation_mode(value)
        except ValueError as exc:
            raise ValueError(
                "security.command_moderation.mode должен быть 'allow', 'ask' или 'deny'"
            ) from exc


class SecurityConfig(BaseModel):
    """
    Четыре независимых слоя безопасности. Все safe-by-default-OFF.
    Структура (clean break c прежней вложенностью под command_moderation):
        security:
          command_moderation: {mode, allowed_command_examples}
          write_guard:        {enabled, extra_paths}
          read_guard:         {enabled, mode, denied_*, allowed_*}
          url_policy:         {enabled, allowed_schemes, url_blacklist}
    """
    model_config = ConfigDict(extra="forbid")
    command_moderation: CommandModerationConfig = Field(
        default_factory=CommandModerationConfig
    )
    write_guard: WriteGuardConfig = Field(default_factory=WriteGuardConfig)
    read_guard: ReadGuardConfig = Field(default_factory=ReadGuardConfig)
    url_policy: UrlPolicyConfig = Field(default_factory=UrlPolicyConfig)


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
        command_moderation["mode"] = "allow"

    write_guard = config_dict.setdefault("write_guard", {})
    if write_guard_enabled is not None:
        write_guard["enabled"] = bool(write_guard_enabled)
    elif "enabled" not in write_guard:
        write_guard["enabled"] = False

    read_guard = config_dict.setdefault("read_guard", {})
    if read_guard_enabled is not None:
        read_guard["enabled"] = bool(read_guard_enabled)
    elif "enabled" not in read_guard:
        read_guard["enabled"] = False
    if read_guard_mode is not None:
        read_guard["mode"] = validate_read_guard_mode(read_guard_mode)
    elif "mode" not in read_guard:
        read_guard["mode"] = "deny"
