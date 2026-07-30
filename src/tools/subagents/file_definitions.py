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

# Имя сабагента = имя тула. Префикс обязателен: по нему `is_subagent_tool`
# отличает вызов сабагента, а `_normalize_agent_kind` отрезает его, получая kind
# для имени лог-файла.
NAME_RE = re.compile(r"^subagent_[a-z0-9_]+$")

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
        raise SubagentDefinitionError(path, "файл должен начинаться с YAML-frontmatter ('---')")

    fm_lines: List[str] = []
    idx = 1
    while idx < len(lines) and lines[idx].strip() != "---":
        fm_lines.append(lines[idx])
        idx += 1
    if idx >= len(lines):
        raise SubagentDefinitionError(path, "frontmatter не закрыт ('---')")

    return "".join(fm_lines), "".join(lines[idx + 1:])


def parse_subagent_file(path: str, text: str) -> SubagentDefinition:
    """Разобрать содержимое файла-определения либо бросить SubagentDefinitionError."""
    fm_text, body = _split_frontmatter(path, text)

    try:
        meta = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise SubagentDefinitionError(path, f"frontmatter не разбирается как YAML: {exc}") from exc
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise SubagentDefinitionError(path, "frontmatter должен быть отображением ключ-значение")

    unknown = sorted(str(key) for key in meta.keys() if str(key) not in _ALLOWED_KEYS)
    if unknown:
        raise SubagentDefinitionError(
            path,
            "неизвестные ключи frontmatter: " + ", ".join(unknown)
            + "; допустимы: " + ", ".join(sorted(_ALLOWED_KEYS)),
        )

    name = str(meta.get("name") or "").strip()
    if not name:
        raise SubagentDefinitionError(path, "обязательное поле 'name' отсутствует или пусто")
    if not NAME_RE.match(name):
        raise SubagentDefinitionError(
            path,
            f"'name' должен матчить {NAME_RE.pattern} (нижний регистр, префикс subagent_): {name!r}",
        )

    description = str(meta.get("description") or "").strip()
    if not description:
        raise SubagentDefinitionError(path, "обязательное поле 'description' отсутствует или пусто")

    role = body.strip()
    if not role:
        raise SubagentDefinitionError(path, "тело файла пусто — сабагент без роли бессмысленен")

    parsed: Dict[str, Optional[Tuple[str, ...]]] = {}
    for key in _LIST_KEYS:
        if key not in meta:
            parsed[key] = None
            continue
        raw = meta.get(key)
        if raw is None:
            parsed[key] = None
            continue
        if not isinstance(raw, list):
            raise SubagentDefinitionError(path, f"'{key}' должен быть списком строк")
        parsed[key] = _normalize_names(raw)

    return SubagentDefinition(
        name=name,
        description=description,
        role=role,
        tools=parsed["tools"],
        stop_tools=parsed["stop_tools"] or (),
        source_path=path,
    )


def _warn(logger: Optional[logging.Logger], msg: str, *args: Any) -> None:
    (logger or _log).warning(msg, *args)


def discover_subagent_definitions(
    config_dict: Dict[str, Any],
    *,
    builtin_names: Sequence[str] | set[str],
    logger: Optional[logging.Logger] = None,
) -> List[SubagentDefinition]:
    """Прочитать папку ``agent.subagents_dir`` и вернуть валидные определения.

    Порядок чтения — по имени файла: он обязан быть детерминированным, иначе
    правило «при коллизии побеждает прочитанный первым» зависело бы от порядка
    выдачи файловой системы. Встроенные сабагенты считаются прочитанными раньше
    любых кастомных, поэтому кастомный с их именем проигрывает.

    Единственный отказ, роняющий старт, — обязательные тулы (см. ``file_subagent``).
    Здесь всё деградирует до варнинга: один битый файл не должен лишать оператора
    остальных.
    """
    raw_dir = (config_dict or {}).get("subagents_dir")
    base_dir = str(raw_dir).strip() if raw_dir is not None else ""
    if not base_dir:
        return []

    if not os.path.isdir(base_dir):
        _warn(logger, "subagents_dir_missing dir=%s (кастомные сабагенты не загружены)", base_dir)
        return []

    try:
        entries = sorted(os.listdir(base_dir))
    except OSError as exc:
        _warn(logger, "subagents_dir_unreadable dir=%s error=%s", base_dir, exc)
        return []

    max_count = _resolve_max_count(config_dict)
    taken: Dict[str, SubagentDefinition] = {}
    builtin = set(builtin_names)
    overflow: List[str] = []

    for entry in entries:
        if not entry.endswith(".md"):
            continue
        path = os.path.join(base_dir, entry)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fd:
                text = fd.read()
            definition = parse_subagent_file(path, text)
        except (OSError, SubagentDefinitionError) as exc:
            _warn(logger, "subagent_definition_invalid error=%s", exc)
            continue

        if definition.name in builtin:
            _warn(
                logger,
                "subagent_name_collision name=%s path=%s (совпадает со встроенным, файл пропущен)",
                definition.name, path,
            )
            continue
        if definition.name in taken:
            _warn(
                logger,
                "subagent_name_collision name=%s path=%s winner=%s (побеждает прочитанный первым)",
                definition.name, path, taken[definition.name].source_path,
            )
            continue

        if len(taken) >= max_count:
            overflow.append(definition.name)
            continue
        taken[definition.name] = definition

    if overflow:
        # Молчаливое усечение читалось бы как «все агенты подхватились».
        _warn(
            logger,
            "subagents_max_count_exceeded limit=%s dropped=%s",
            max_count, ", ".join(overflow),
        )

    return list(taken.values())


def _resolve_max_count(config_dict: Dict[str, Any]) -> int:
    raw = (config_dict or {}).get("subagents_max_count", DEFAULT_MAX_COUNT)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_COUNT
    return value if value > 0 else DEFAULT_MAX_COUNT


__all__ = [
    "DEFAULT_MAX_COUNT",
    "NAME_RE",
    "SubagentDefinition",
    "SubagentDefinitionError",
    "discover_subagent_definitions",
    "parse_subagent_file",
]
