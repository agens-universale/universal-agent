"""Межполевые проверки итогового конфига (фаза 2, после мержа слоёв).

Сюда — только сочетания полей, которые по отдельности законны, а вместе
бессмысленны, и только те, что рантайм сегодня молча обходит (warning в лог,
пропуск, «мёртвое» плечо). Проверка идёт по merged-конфигу: половина пары вправе
лежать в другом слое (`cert_path` в базе, `key_path` в проекте), поэтому
валидатором модели (фаза 1, каждый файл отдельно — в том числе частичный
профиль) это не сделать.

Исход — стартовые notices (`runtime.notices`), не отказ: сегодня все эти
сочетания запускаются, и запирать человека снаружи приложения из-за них было бы
регрессом. Отказами стали только однозначные поломки, которые проверяются по
одному полю, — валидаторами секций: пустой путь в списках guard'ов
(`security.reject_blank_paths`), негодный regex маскировки
(`logging.redact_secrets.extra_patterns`).
"""

from __future__ import annotations

from typing import Any

from gc_core.config_modules.root import AppConfig

_STDIO_TRANSPORTS = ("stdio", "stream_stdio")


def _half_pair(cert: Any, key: Any) -> str | None:
    if bool(cert) == bool(key):
        return None
    return "cert" if cert else "key"


def _mtls_notices(cfg: AppConfig) -> list[str]:
    return [
        *_backend_mtls_notices(cfg),
        *_model_mtls_notices(cfg),
        *_mcp_mtls_notices(cfg),
    ]


def _backend_mtls_notices(cfg: AppConfig) -> list[str]:
    """Половина пары cert/key у общего ``backend``."""
    backend = cfg.backend
    half = _half_pair(backend.cert_path, backend.key_path)
    if not half:
        return []
    given, missing = (
        ("backend.cert_path", "backend.key_path")
        if half == "cert"
        else ("backend.key_path", "backend.cert_path")
    )
    return [
        f"{given} задан без {missing} — клиентский сертификат (mTLS) не "
        "используется; задайте оба или уберите оба"
    ]


def _model_mtls_notices(cfg: AppConfig) -> list[str]:
    """Половина пары у модели с учётом унаследованного из общего ``backend``."""
    notices: list[str] = []
    backend = cfg.backend
    for alias, entry in cfg.models.items():
        own = entry.backend
        if own is None:
            continue
        explicit = own.model_fields_set
        cert = own.cert_path if "cert_path" in explicit else backend.cert_path
        key = own.key_path if "key_path" in explicit else backend.key_path
        if not ({"cert_path", "key_path"} & explicit):
            continue  # пара целиком унаследована — о ней сказано выше
        if _half_pair(cert, key):
            notices.append(
                f"models.{alias}.backend: с учётом общего backend задан только "
                "один из cert_path/key_path — клиентский сертификат (mTLS) для "
                "этой модели не используется"
            )
    return notices


def _mcp_mtls_notices(cfg: AppConfig) -> list[str]:
    """Половина пары client-cert/client-key у MCP-серверов."""
    notices: list[str] = []
    for name, server in cfg.mcp.servers.items():
        half = _half_pair(server.client_cert, server.client_key)
        if half:
            given, missing = (
                ("client-cert", "client-key")
                if half == "cert"
                else ("client-key", "client-cert")
            )
            notices.append(
                f"mcp.servers.{name}.{given} задан без {missing} — клиентский "
                "сертификат (mTLS) не используется"
            )
    return notices


def _mcp_endpoint_notices(cfg: AppConfig) -> list[str]:
    notices: list[str] = []
    for name, server in cfg.mcp.servers.items():
        if server.transport in _STDIO_TRANSPORTS:
            if not server.command:
                notices.append(
                    f"mcp.servers.{name}.command: не задан для transport "
                    f"{server.transport} — сервер будет пропущен"
                )
        elif not server.url:
            notices.append(
                f"mcp.servers.{name}.url: не задан для transport "
                f"{server.transport} — сервер будет пропущен"
            )
    return notices


def _response_tokens_notices(cfg: AppConfig) -> list[str]:
    notices: list[str] = []
    backend = cfg.backend

    def check(prefix: str, minimum: int, maximum: int) -> None:
        if minimum > maximum:
            notices.append(
                f"{prefix}chat_completions_min_response_tokens ({minimum}) больше "
                f"{prefix}chat_completions_max_tokens ({maximum}) — повтор после "
                "переполнения контекста будет ПОДНИМАТЬ max_tokens, а не снижать"
            )

    check(
        "backend.",
        backend.chat_completions_min_response_tokens,
        backend.chat_completions_max_tokens,
    )
    for alias, entry in cfg.models.items():
        own = entry.backend
        if own is None:
            continue
        explicit = own.model_fields_set
        if not (
            {
                "chat_completions_min_response_tokens",
                "chat_completions_max_tokens",
            }
            & explicit
        ):
            continue
        check(
            f"models.{alias}.backend.",
            own.chat_completions_min_response_tokens
            if "chat_completions_min_response_tokens" in explicit
            else backend.chat_completions_min_response_tokens,
            own.chat_completions_max_tokens
            if "chat_completions_max_tokens" in explicit
            else backend.chat_completions_max_tokens,
        )
    return notices


def _summary_notices(cfg: AppConfig) -> list[str]:
    notices: list[str] = []
    summary = cfg.summary
    for scale in ("chars", "tokens"):
        mid = getattr(summary, f"chat_completions_summary_trigger_{scale}")
        end = getattr(
            summary, f"chat_completions_summary_turn_end_trigger_{scale}"
        )
        if end is not None and mid > 0 and end >= mid:
            notices.append(
                f"summary.chat_completions_summary_turn_end_trigger_{scale} "
                f"({end}) не меньше summary.chat_completions_summary_trigger_"
                f"{scale} ({mid}) — компакция в конце хода не сработает раньше "
                "компакции посреди хода; порог конца хода задают меньше"
            )
    return notices


def _project_memory_notices(cfg: AppConfig) -> list[str]:
    memory = cfg.project_memory
    if 0 < memory.max_chars < memory.max_append_chars:
        return [
            f"project_memory.max_append_chars ({memory.max_append_chars}) больше "
            f"project_memory.max_chars ({memory.max_chars}) — дописка крупнее "
            "всей памяти проекта не поместится и после компакции"
        ]
    return []


def cross_field_notices(cfg: AppConfig) -> list[str]:
    """Предупреждения о бессмысленных сочетаниях полей итогового конфига.

    Зовётся загрузчиком (`config.load_settings`) ДО `resolve_paths`: явные
    значения ещё не заменены выведенными умолчаниями (порог конца хода `None`
    — «выведи сам», а не «равен середине хода»)."""
    return (
        _mtls_notices(cfg)
        + _mcp_endpoint_notices(cfg)
        + _response_tokens_notices(cfg)
        + _summary_notices(cfg)
        + _project_memory_notices(cfg)
    )


__all__ = ["cross_field_notices"]
