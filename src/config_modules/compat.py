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

# {"старый.dotted.путь": "новый.dotted.путь"}
RENAMES: Dict[str, str] = {}
# опциональное преобразование значения при переносе (смена формата/смысла)
RENAME_CONVERTERS: Dict[str, Callable[[Any], Any]] = {}
# {"dotted.путь": "почему удалён"}
REMOVED: Dict[str, str] = {
    "update.check_interval_days": "проверка идёт при каждом запуске; выключить — update.check: off",
}

_SYNC_HINT = "обновите config.yml: gc-cli --sync-config"


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
                    f"{old}: значение не удалось перенести в {new} — "
                    f"задайте заново ({_SYNC_HINT})")
                continue
        if not explicit_new:
            _set_path(raw, new, value)
            facts.append(f"{old} переименован в {new} — {_SYNC_HINT}")
        else:
            facts.append(
                f"{old} устарел: действует явно заданный {new}, "
                f"старое значение отброшено — {_SYNC_HINT}"
            )
    for old, why in REMOVED.items():
        present, _ = _pop_path(raw, old)
        if present:
            facts.append(f"{old} устарел и игнорируется ({why}) — {_SYNC_HINT}")
    return facts
