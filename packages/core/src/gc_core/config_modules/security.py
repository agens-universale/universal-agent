from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gc_core.config_modules.field_meta import gc_meta

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


def reject_blank_paths(values: list[str]) -> list[str]:
    """Пустой/пробельный элемент в списке путей guard'а — ошибка схемы.

    Пустая строка не «ничего»: путевые политики раскрывают её в ТЕКУЩИЙ
    каталог процесса (`os.path.abspath("")`). В `read_guard.allowed_read_paths`
    это снимало все запреты (`.env`, ключи) для всего cwd; в
    `write_guard.extra_paths` — роняло каждую охраняемую запись
    (`path must not be empty`); в `protected_paths.extra` — объявляло
    защищённым весь cwd. Типичный источник — `${ENV:VAR}`, раскрывшийся в
    пустоту, поэтому отказ называет индекс элемента; pydantic добавит путь
    поля (`security.read_guard.allowed_read_paths: …`)."""
    blank = [i for i, value in enumerate(values) if not str(value).strip()]
    if blank:
        indices = ", ".join(f"[{i}]" for i in blank)
        raise ValueError(
            f"пустой путь в списке (элемент {indices}) — пустая строка "
            "раскрывается в текущий каталог процесса; уберите элемент или "
            "впишите путь"
        )
    return values


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

    model_config = ConfigDict(extra="forbid", frozen=True)
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

    @field_validator("extra_paths")
    @classmethod
    def validate_extra_paths(cls, value: list[str]) -> list[str]:
        return reject_blank_paths(value)


class UrlPolicyConfig(BaseModel):
    """
    URL-policy для fetch_url/simple_fetch_url. On/ask по дефолту (secure-by-default).

    mode:
      deny  — нарушение → блок с `url_blocked_by_policy`;
      ask   — нарушение → запрос подтверждения у пользователя (interactive);
      allow — нарушение → пропустить, audit log пометит `out_of_perimeter`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
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

    model_config = ConfigDict(extra="forbid", frozen=True)
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
    max_file_size_kb: int = Field(
        default=1024, json_schema_extra=gc_meta(expert=True)
    )
    binary_read_policy: str = Field(
        default="ask", json_schema_extra=gc_meta(expert=True)
    )
    binary_allowed_types: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_BINARY_ALLOWED_TYPES),
        json_schema_extra=gc_meta(expert=True),
    )
    binary_allowed_extensions: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_BINARY_ALLOWED_EXTENSIONS),
        json_schema_extra=gc_meta(expert=True),
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

    @field_validator(
        "allowed_read_roots", "allowed_read_paths", "denied_read_paths"
    )
    @classmethod
    def validate_path_lists(cls, value: list[str]) -> list[str]:
        return reject_blank_paths(value)


class DownloadConfig(BaseModel):
    """Настройки тула download_file: каталог загрузки и лимит размера тела ответа."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    download_dir: str = "downloads"
    max_bytes: int = 52_428_800


class CommandModerationConfig(BaseModel):
    """
    Judge-настройки для execute_command/run_python_code. On/ask по дефолту (secure-by-default).

    enabled=False — слой выключен полностью, judge не вызывается (всё проходит).
    enabled=True + mode=allow — то же самое (без судьи).
    enabled=True + mode=ask/deny — судья активен.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
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

    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = True
    extra: list[str] = Field(default_factory=list)

    @field_validator("extra")
    @classmethod
    def validate_extra(cls, value: list[str]) -> list[str]:
        return reject_blank_paths(value)


class PermissionsConfig(BaseModel):
    """Режим разрешений: единый рычаг автономии агента.

    default_mode — с какого режима стартует сессия; выбор в сессии (Shift+Tab)
    и флаг запуска ``--permission-mode`` сильнее. readonly_commands дополняет
    встроенный список команд, исполняемых без подтверждения в любом режиме.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    default_mode: str = "edit"
    readonly_commands: list[str] = Field(default_factory=list)

    @field_validator("default_mode")
    @classmethod
    def validate_default_mode(cls, value: str) -> str:
        # Lazy: резолвер режимов сам читает настройки (gc_core.settings →
        # config_modules) — импорт на уровне модуля замкнул бы цикл.
        from gc_core.security.permission_mode import AVAILABLE_MODES

        mode = str(value or "").strip().lower()
        allowed = {item.value for item in AVAILABLE_MODES}
        if mode not in allowed:
            raise ValueError(
                "security.permissions.default_mode должен быть одним из: "
                + ", ".join(sorted(allowed))
            )
        return mode


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
          permissions:        {default_mode, readonly_commands}
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    command_moderation: CommandModerationConfig = Field(
        default_factory=CommandModerationConfig
    )
    write_guard: WriteGuardConfig = Field(default_factory=WriteGuardConfig)
    read_guard: ReadGuardConfig = Field(default_factory=ReadGuardConfig)
    url_policy: UrlPolicyConfig = Field(default_factory=UrlPolicyConfig)
    download: DownloadConfig = Field(
        default_factory=DownloadConfig, json_schema_extra=gc_meta(expert=True)
    )
    protected_paths: ProtectedPathsConfig = Field(
        default_factory=ProtectedPathsConfig
    )
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
