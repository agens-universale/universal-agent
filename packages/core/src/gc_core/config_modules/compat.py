"""Compat-слой устаревших ключей user config.yml (спека A, A4).

Применяется в load_config ДО AppConfig.model_validate: старые ключи на лету
переносятся/выбрасываются in-memory, файл юзера НЕ трогается (его чинит
--sync-config). Без этого extra="forbid" уронит старый конфиг при первом же
переименовании ключа схемы.

ПРАВИЛО СОПРОВОЖДЕНИЯ (чек-лист релиза): удаляешь/переименовываешь ключ
схемы → обязан добавить запись сюда.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

# {"старый.dotted.путь": "новый.dotted.путь"}  # noqa: ERA001  # описание формата, не выключенный код
RENAMES: Dict[str, str] = {
    # Порядок значим: записи применяются по очереди. Сначала секция целиком
    # (`diff_tui` — запись правок давно в ядре и идёт не только в TUI), затем её
    # старые листья — уже под новым именем.
    "diff_tui": "agent_edits",
    # лимит снимков «до правки» уехал из cache-mode legacy-стора в .gc/agent_edits
    "agent_edits.cache_max_orig": "agent_edits.snapshots_max_files",
    # AliasChoices("auxiliary_llm", "llm") недостаточно: deep_merge user(llm)+project
    # (auxiliary_llm) оставляет оба ключа → extra=forbid «Extra inputs are not permitted».
    "chat_completions.llm": "chat_completions.auxiliary_llm",
}
# опциональное преобразование значения при переносе (смена формата/смысла)
RENAME_CONVERTERS: Dict[str, Callable[[Any], Any]] = {}
# Переименованные СЕКЦИИ, которые при обоих заданных именах сливаются (новое
# побеждает), а не теряют старую целиком. Только для секций из независимых
# ключей: у `chat_completions.llm` → `auxiliary_llm` это настройки модели, и
# слияние смешало бы параметры двух разных моделей — там старая отбрасывается.
MERGE_RENAMED_SECTIONS = frozenset({"diff_tui"})
# {"dotted.путь": "почему удалён"}  # noqa: ERA001  # описание формата, не выключенный код
# Сегмент ``*`` — любое имя записи словаря (алиас модели и т.п.).
REMOVED: Dict[str, str] = {
    "update.check_interval_days": "проверка идёт при каждом запуске; выключить — update.check: off",
    "logging.dump_storage": "debug-дамп storage удалён; обновите config.yml: gc-cli --sync-config",
    "agent_edits.preview_context_lines": "экран правок считает дифф по журналу; контекст хэнков — agent_edits.hunk_context_lines",
    "agent_edits.full_file_max_lines": "порога больше нет: клавиша f на экране правок показывает файл целиком",
    # На уровне модели никогда не применялись: протокол, поток и строгость
    # схем тулов действуют на весь бэкенд.
    "models.*.backend.api_mode": "на уровне модели не применяется — задайте в общей секции backend",
    "models.*.backend.stream": "на уровне модели не применяется — задайте в общей секции backend",
}

_SYNC_HINT = "обновите config.yml: gc-cli --sync-config"
# `--sync-config` обновляет только пользовательский config.yml: проектный слой,
# config.local.yml и профили он не трогает, и совет запустить его про такой файл
# вёл бы в тупик.
_MANUAL_HINT = "переименуйте ключ в этом файле вручную"


def manual_edit_fact(fact: str) -> str:
    """Факт compat про файл, который ``--sync-config`` не обновляет."""
    return fact.replace(_SYNC_HINT, _MANUAL_HINT)


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


def _get_path(d: Dict[str, Any], dotted: str) -> Any:
    cur: Any = d
    for p in dotted.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _deep_merge_missing(
    target: Dict[str, Any], extra: Dict[str, Any]
) -> Dict[str, Any]:
    """``target`` дополняется ключами ``extra``, которых в нём нет; свои не трогаются."""
    for key, value in extra.items():
        if key not in target:
            target[key] = value
        elif isinstance(target[key], dict) and isinstance(value, dict):
            _deep_merge_missing(target[key], value)
    return target


def map_legacy_path(dotted: str) -> Optional[str]:
    """Куда `apply_compat` унесёт ключ ``dotted``; ``None`` — ключ удалён.

    Переименования применяются по очереди и к самому пути, и к его предкам: лист
    старой секции (``diff_tui.cache_max_orig``) проходит сначала перенос секции,
    затем свой собственный. Нужно `--sync-config`: он переносит конфиг по листьям,
    и лист, перенесённый только по имени секции, оставил бы удалённый ключ в файле.
    """
    path = dotted
    for old, new in RENAMES.items():
        if path == old or path.startswith(old + "."):
            path = new + path[len(old) :]
    # ``*`` в REMOVED — любое имя записи (``models.*.backend.stream``).
    if any(matches_pattern(path, gone, prefix=True) for gone in REMOVED):
        return None
    return path


def apply_compat(raw: Dict[str, Any]) -> List[str]:
    """Мутирует raw. Возвращает человекочитаемые факты для startup-notices."""
    facts: List[str] = []
    _compat_model_kwargs(raw, facts)
    _compat_renames(raw, facts)
    _compat_removed(raw, facts)
    return facts


def _compat_model_kwargs(raw: Dict[str, Any], facts: List[str]) -> None:
    """``models.<alias>.backend.chat_completion_kwargs`` → ``models.<alias>.main``."""
    models = raw.get("models")
    for alias, entry in models.items() if isinstance(models, dict) else ():
        if not isinstance(entry, dict):
            continue
        backend = entry.get("backend")
        if (
            not isinstance(backend, dict)
            or "chat_completion_kwargs" not in backend
        ):
            continue
        legacy = backend.pop("chat_completion_kwargs")
        if "main" not in entry:
            entry["main"] = legacy
        facts.append(
            f"models.{alias}.backend.chat_completion_kwargs переименован в "
            f"models.{alias}.main; явно заданный main имеет приоритет — "
            "сохраните запись модели в настройках или обновите файл вручную"
        )


def _compat_renames(raw: Dict[str, Any], facts: List[str]) -> None:
    """Переименованные ключи (``RENAMES``) — с конвертером значения, если он есть."""
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
        facts.append(_place_renamed(raw, old, new, value, explicit_new))


def _compat_removed(raw: Dict[str, Any], facts: List[str]) -> None:
    """Удалённые ключи (``REMOVED``) снимаются из документа с фактом."""
    for old, why in REMOVED.items():
        for concrete in expand_path(raw, old):
            present, _ = pop_parts(raw, concrete)
            if present:
                facts.append(
                    f"{'.'.join(concrete)} устарел и игнорируется ({why}) — {_SYNC_HINT}"
                )


def _place_renamed(
    raw: Dict[str, Any], old: str, new: str, value: Any, explicit_new: bool
) -> str:
    """Положить значение старого имени под новое; факт для уведомления."""
    if not explicit_new:
        _set_path(raw, new, value)
        return f"{old} переименован в {new} — {_SYNC_HINT}"
    if (
        old in MERGE_RENAMED_SECTIONS
        and isinstance(value, dict)
        and isinstance(_get_path(raw, new), dict)
    ):
        # Переименованная СЕКЦИЯ, заданная под обоими именами — обычно в
        # разных слоях конфига (старое в пользовательском, новое в проектном):
        # выбросить старую целиком значило бы молча потерять её ключи.
        # Сливаем под новую, заданное в новой побеждает.
        _set_path(raw, new, _deep_merge_missing(_get_path(raw, new), value))
        return (
            f"{old} переименован в {new}: ключи слиты, заданные в {new} "
            f"побеждают — {_SYNC_HINT}"
        )
    return f"{old} устарел: действует явно заданный {new}, старое значение отброшено — {_SYNC_HINT}"
