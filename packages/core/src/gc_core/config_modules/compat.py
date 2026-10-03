"""Compat-слой устаревших ключей user config.yml (спека A, A4).

Применяется в load_config ДО AppConfig.model_validate: старые ключи на лету
переносятся/выбрасываются in-memory, файл юзера НЕ трогается (его чинит
--sync-config). Без этого extra="forbid" уронит старый конфиг при первом же
переименовании ключа схемы.

ПРАВИЛО СОПРОВОЖДЕНИЯ (чек-лист релиза): удаляешь/переименовываешь ключ
схемы → обязан добавить запись сюда.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

# {"старый.dotted.путь": "новый.dotted.путь"}  # noqa: ERA001  # описание формата, не выключенный код
RENAMES: Dict[str, str] = {
    # AliasChoices("auxiliary_llm", "llm") недостаточно: deep_merge user(llm)+project
    # (auxiliary_llm) оставляет оба ключа → extra=forbid «Extra inputs are not permitted».
    "chat_completions.llm": "chat_completions.auxiliary_llm",
}
# опциональное преобразование значения при переносе (смена формата/смысла)
RENAME_CONVERTERS: Dict[str, Callable[[Any], Any]] = {}
# {"dotted.путь": "почему удалён"}  # noqa: ERA001  # описание формата, не выключенный код
# Сегмент ``*`` — любое имя записи словаря (алиас модели и т.п.).
REMOVED: Dict[str, str] = {
    "update.check_interval_days": "проверка идёт при каждом запуске; выключить — update.check: off",
    "logging.dump_storage": "debug-дамп storage удалён; обновите config.yml: gc-cli --sync-config",
    # На уровне модели никогда не применялись: протокол, поток и строгость
    # схем тулов действуют на весь бэкенд.
    "models.*.backend.api_mode": "на уровне модели не применяется — задайте в общей секции backend",
    "models.*.backend.stream": "на уровне модели не применяется — задайте в общей секции backend",
    "models.*.backend.send_tool_strict": "на уровне модели не применяется — задайте в общей секции backend",
    "models.*.backend.openai_strict_tools_schema": "на уровне модели не применяется — задайте в общей секции backend",
}

_SYNC_HINT = "обновите config.yml: gc-cli --sync-config"


def expand_path(d: Dict[str, Any], pattern: str) -> List[Tuple[str, ...]]:
    """Пути документа (сегментами), совпадающие с ``pattern``; ``*`` — любое
    имя записи. Сегменты, а не строка: алиас модели может содержать точку."""
    found: List[Tuple[Tuple[str, ...], Any]] = [((), d)]
    for part in pattern.split("."):
        found = [
            ((*path, str(key)), node[key])
            for path, node in found
            if isinstance(node, dict)
            for key in (list(node) if part == "*" else [part])
            if key in node
        ]
    return [path for path, _ in found]


def pop_parts(d: Dict[str, Any], parts: Tuple[str, ...]) -> Tuple[bool, Any]:
    """Снять значение по пути-сегментам (без разбора строки по точкам)."""
    cur: Any = d
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    if not isinstance(cur, dict) or parts[-1] not in cur:
        return False, None
    return True, cur.pop(parts[-1])


def matches_pattern(
    dotted: str, pattern: str, *, prefix: bool = False
) -> bool:
    """Путь совпадает с шаблоном (``*`` — любой сегмент); ``prefix`` — путь
    лежит внутри шаблона (сам шаблон или его потомок)."""
    parts, pat = dotted.split("."), pattern.split(".")
    if len(parts) < len(pat) or (not prefix and len(parts) != len(pat)):
        return False
    return all(p in ("*", q) for p, q in zip(pat, parts))


def _pop_path(d: Dict[str, Any], dotted: str) -> Tuple[bool, Any]:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return False, None
        cur = cur[p]
    if not isinstance(cur, dict) or parts[-1] not in cur:
        return False, None
    return True, cur.pop(parts[-1])


def _has_path(d: Dict[str, Any], dotted: str) -> bool:
    cur = d
    for p in dotted.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return False
        cur = cur[p]
    return True


def _set_path(d: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def apply_compat(raw: Dict[str, Any]) -> List[str]:
    """Мутирует raw. Возвращает человекочитаемые факты для startup-notices."""
    facts: List[str] = []
    for old, new in RENAMES.items():
        explicit_new = _has_path(raw, new)
        present, value = _pop_path(raw, old)
        if not present:
            continue
        conv = RENAME_CONVERTERS.get(old)
        if conv is not None:
            try:
                value = conv(value)
            except Exception:
                facts.append(
                    f"{old}: значение не удалось перенести в {new} — задайте заново ({_SYNC_HINT})"
                )
                continue
        if not explicit_new:
            _set_path(raw, new, value)
            facts.append(f"{old} переименован в {new} — {_SYNC_HINT}")
        else:
            facts.append(
                f"{old} устарел: действует явно заданный {new}, старое значение отброшено — {_SYNC_HINT}"
            )
    for old, why in REMOVED.items():
        for concrete in expand_path(raw, old):
            present, _ = pop_parts(raw, concrete)
            if present:
                facts.append(
                    f"{'.'.join(concrete)} устарел и игнорируется ({why}) — {_SYNC_HINT}"
                )
    return facts
