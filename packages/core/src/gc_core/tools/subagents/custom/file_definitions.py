"""Кастомные сабагенты, описанные файлами: парсинг, валидация, дискавери.

Файл — YAML-frontmatter + тело. Тело играет роль ``agent_role``: оно подставляется
в ``<specific_role>`` общего шаблона промпта сабагента, а не заменяет шаблон.

Модуль намеренно не импортирует ``registry``: тот импортирует адаптер, который
импортирует этот модуль, и обратная связь замкнула бы граф.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from gc_core.agent_store import (
    AgentResource,
    agent_store_of,
    read_resource_text,
)
from gc_core.settings import settings_of

# Имя сабагента = имя тула. Префикс обязателен: по нему `is_subagent_tool`
# отличает вызов сабагента, а `_normalize_agent_kind` отрезает его, получая kind
# для имени лог-файла.
NAME_RE = re.compile(r"^subagent_[a-z0-9_]+$")

# Предел длины имени. Два независимых основания, оба жёсткие:
# - OpenAI-совместимые бэкенды ограничивают имя функции 64 символами, то есть
#   более длинное имя отверг бы сам API;
# - имя без префикса становится `kind` в имени лог-файла
#   (`agent_<kind>_<run_id>.jsonl`), а basename ограничен 255 байтами. Без лимита
#   длинное имя даёт `OSError: File name too long` не на старте, а в момент вызова
#   сабагента — то есть в самый неудобный момент.
MAX_NAME_LEN = 64
# Предел длины описания. Оно уходит в схему тулов на КАЖДОМ запросе к модели,
# поэтому неограниченный текст — это постоянный расход контекста. Порог с большим
# запасом: у встроенных сабагентов описания 220-280 символов.
MAX_DESCRIPTION_LEN = 1024

_ALLOWED_KEYS = frozenset({"name", "description", "tools", "stop_tools"})
_LIST_KEYS = ("tools", "stop_tools")

DEFAULT_MAX_COUNT = 20

_log = logging.getLogger(__name__)


class SubagentDefinitionError(ValueError):
    """Файл-определение невалиден. Несёт путь, чтобы оператор знал, что чинить."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class SubagentDefinition:
    """Разобранное определение кастомного сабагента.

    ``tools is None`` и ``tools == ()`` — разные вещи: первое означает «ключа нет,
    доступны все тулы», второе — «явно ни одного». Второе гарантированно упрётся в
    проверку обязательных тулов, и это правильно: пустой список почти наверняка
    ошибка оператора, но отвергать его должен валидатор тулов, а не парсер.
    """

    name: str
    description: str
    role: str
    tools: Optional[Tuple[str, ...]]
    stop_tools: Tuple[str, ...]
    source_path: str


def _normalize_names(values: Iterable[Any]) -> Tuple[str, ...]:
    """Тримминг, отбрасывание пустых, дедуп с сохранением порядка.

    Та же нормализация, что у ``_apply_stop_tools`` (``tools/registry.py``), чтобы
    список из файла и глобальный ``agent.stop_tools`` вели себя одинаково.
    """
    out: List[str] = []
    for raw in values:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if name and name not in out:
            out.append(name)
    return tuple(out)


def _split_frontmatter(path: str, text: str) -> Tuple[str, str]:
    """Разделить файл на YAML-frontmatter и тело.

    В отличие от скиллов (там отсутствие frontmatter — не ошибка, тело = весь файл),
    здесь frontmatter обязателен: без ``name`` сабагента не существует, и молча
    считать весь файл ролью значило бы получить безымянного агента.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SubagentDefinitionError(
            path, "файл должен начинаться с YAML-frontmatter ('---')"
        )

    fm_lines: List[str] = []
    idx = 1
    while idx < len(lines) and lines[idx].strip() != "---":
        fm_lines.append(lines[idx])
        idx += 1
    if idx >= len(lines):
        raise SubagentDefinitionError(path, "frontmatter не закрыт ('---')")

    return "".join(fm_lines), "".join(lines[idx + 1 :])


def _require_name(path: str, meta: Dict[str, Any]) -> str:
    name = str(meta.get("name") or "").strip()
    if not name:
        raise SubagentDefinitionError(
            path, "обязательное поле 'name' отсутствует или пусто"
        )
    if not NAME_RE.match(name):
        raise SubagentDefinitionError(
            path,
            f"'name' должен матчить {NAME_RE.pattern} (нижний регистр, префикс subagent_): {name!r}",
        )
    if len(name) > MAX_NAME_LEN:
        raise SubagentDefinitionError(
            path,
            f"'name' длиннее {MAX_NAME_LEN} символов ({len(name)}): лимит имени функции "
            "у OpenAI-совместимых бэкендов и защита имени лог-файла",
        )
    return name


def _require_description(path: str, meta: Dict[str, Any]) -> str:
    description = str(meta.get("description") or "").strip()
    if not description:
        raise SubagentDefinitionError(
            path, "обязательное поле 'description' отсутствует или пусто"
        )
    if len(description) > MAX_DESCRIPTION_LEN:
        raise SubagentDefinitionError(
            path,
            f"'description' длиннее {MAX_DESCRIPTION_LEN} символов ({len(description)}): "
            "описание уходит в схему тулов на каждом запросе к модели",
        )
    return description


def _parse_list_key(
    path: str, meta: Dict[str, Any], key: str
) -> Optional[Tuple[str, ...]]:
    """Список имён из ключа frontmatter; None — ключа нет или он null."""
    raw = meta.get(key)
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise SubagentDefinitionError(
            path, f"'{key}' должен быть списком строк"
        )
    return _normalize_names(raw)


def parse_subagent_file(path: str, text: str) -> SubagentDefinition:
    """Разобрать содержимое файла-определения либо бросить SubagentDefinitionError."""
    fm_text, body = _split_frontmatter(path, text)

    try:
        meta = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise SubagentDefinitionError(
            path, f"frontmatter не разбирается как YAML: {exc}"
        ) from exc
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise SubagentDefinitionError(
            path, "frontmatter должен быть отображением ключ-значение"
        )

    unknown = sorted(str(key) for key in meta if str(key) not in _ALLOWED_KEYS)
    if unknown:
        raise SubagentDefinitionError(
            path,
            "неизвестные ключи frontmatter: "
            + ", ".join(unknown)
            + "; допустимы: "
            + ", ".join(sorted(_ALLOWED_KEYS)),
        )

    name = _require_name(path, meta)
    description = _require_description(path, meta)

    role = body.strip()
    if not role:
        raise SubagentDefinitionError(
            path, "тело файла пусто — сабагент без роли бессмысленен"
        )

    parsed = {key: _parse_list_key(path, meta, key) for key in _LIST_KEYS}

    return SubagentDefinition(
        name=name,
        description=description,
        role=role,
        tools=parsed["tools"],
        stop_tools=parsed["stop_tools"] or (),
        source_path=path,
    )


def _warn(
    logger: Optional[logging.Logger],
    msg: str,
    *args: Any,
    event: str = "",
    diagnostics: Optional[List[Dict[str, Any]]] = None,
    **fields: Any,
) -> None:
    """Варнинг в логгер рана с машиночитаемым ``event``.

    Значения дублируются и в тексте сообщения, и в ``extra``: поля из ``extra``
    рендерит только ``JsonlFormatter``, а при ``logging.format: text`` (дефолт) в
    строке осталось бы голое имя события.

    ``diagnostics`` (опц.) — тот же факт в структурном виде для стартовых
    уведомлений TUI: лог читают не все, а «файл молча пропущен» оператор обязан
    увидеть на экране.
    """
    extra = {"event": event or "subagent_definition_warning"}
    extra.update(fields)
    (logger or _log).warning(msg, *args, extra=extra)
    if diagnostics is not None:
        diagnostics.append({"code": extra["event"], **fields})


def discover_subagent_definitions(
    settings: Any,
    *,
    builtin_names: Sequence[str] | set[str],
    logger: Optional[logging.Logger] = None,
    diagnostics: Optional[List[Dict[str, Any]]] = None,
) -> List[SubagentDefinition]:
    """Прочитать папку ``agent.subagents_dir`` и вернуть валидные определения.

    Возвращает ВСЕ валидные определения в порядке имён файлов. Порядок обязан
    быть детерминированным, иначе правило «при коллизии побеждает прочитанный
    первым» зависело бы от выдачи файловой системы. Встроенные считаются
    прочитанными раньше любых кастомных, поэтому кастомный с их именем проигрывает.

    ``subagents_max_count`` здесь НЕ применяется: лимит и порядок зависят от
    allowlist'а, а он известен уровнем выше (см. ``registry``).

    Всё деградирует до варнинга: один битый файл не должен лишать оператора
    остальных.

    ``settings`` — всё, что принимает ``settings_of``; ``None`` — папки нет.
    """
    raw_dir = (
        settings_of(settings).agent.subagents_dir
        if settings is not None
        else None
    )
    base_dir = str(raw_dir).strip() if raw_dir is not None else ""
    store = agent_store_of(settings)
    directory = AgentResource("subagents", "", base_dir or None)

    try:
        entries = [
            name for name, kind in store.list_dir(directory) if kind == "file"
        ]
    except (FileNotFoundError, NotADirectoryError):
        # Папка не задана — кастомных нет (своё хранилище может отдать их и
        # без пути); задана, но её нет — предупреждение.
        if base_dir:
            _warn(
                logger,
                "subagents_dir_missing dir=%s (кастомные сабагенты не загружены)",
                base_dir,
                event="subagents_dir_missing",
                diagnostics=diagnostics,
                dir=base_dir,
            )
        return []
    except OSError as exc:
        _warn(
            logger,
            "subagents_dir_unreadable dir=%s error=%s",
            directory.label,
            exc,
            event="subagents_dir_unreadable",
            diagnostics=diagnostics,
            dir=directory.label,
        )
        return []

    taken: Dict[str, SubagentDefinition] = {}
    builtin = set(builtin_names)

    for entry in sorted(entries):
        if not entry.endswith(".md"):
            continue
        path = os.path.join(base_dir, entry) if base_dir else entry
        try:
            # Файл определения пишет человек — значит и кодировка его, а не наша.
            # `UnicodeDecodeError` подкласс `ValueError`, а не `OSError`, и мимо
            # этого перехвата уходил наружу: один файл в чужой кодировке ронял
            # весь старт агента, хотя обещание модуля — «битый файл не лишает
            # оператора остальных».
            definition = parse_subagent_file(
                path,
                read_resource_text(
                    store,
                    AgentResource(
                        "subagents", entry, path if base_dir else None
                    ),
                ),
            )
        except (
            OSError,
            ValueError,
        ) as exc:  # ValueError ⊇ SubagentDefinitionError
            _warn(
                logger,
                "subagent_definition_invalid error=%s",
                exc,
                event="subagent_definition_invalid",
                diagnostics=diagnostics,
                path=path,
                reason=getattr(exc, "reason", str(exc)),
            )
            continue

        if definition.name in builtin:
            _warn(
                logger,
                "subagent_name_collision name=%s path=%s (совпадает со встроенным, файл пропущен)",
                definition.name,
                path,
                event="subagent_name_collision",
                diagnostics=diagnostics,
                subagent_name=definition.name,
                path=path,
                winner="builtin",
            )
            continue
        if definition.name in taken:
            _warn(
                logger,
                "subagent_name_collision name=%s path=%s winner=%s (побеждает прочитанный первым)",
                definition.name,
                path,
                taken[definition.name].source_path,
                event="subagent_name_collision",
                diagnostics=diagnostics,
                subagent_name=definition.name,
                path=path,
                winner=taken[definition.name].source_path,
            )
            continue

        taken[definition.name] = definition

    return list(taken.values())


def resolve_max_count(settings: Any) -> int:
    """``agent.subagents_max_count`` настроек (всё, что принимает ``settings_of``)."""
    raw = (
        settings_of(settings).agent.subagents_max_count
        if settings is not None
        else DEFAULT_MAX_COUNT
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_COUNT
    return value if value > 0 else DEFAULT_MAX_COUNT


__all__ = [
    "DEFAULT_MAX_COUNT",
    "MAX_DESCRIPTION_LEN",
    "MAX_NAME_LEN",
    "NAME_RE",
    "SubagentDefinition",
    "SubagentDefinitionError",
    "discover_subagent_definitions",
    "parse_subagent_file",
    "resolve_max_count",
]
