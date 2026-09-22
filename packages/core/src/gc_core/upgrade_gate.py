"""Политика «первый запуск после апгрейда» (спека A, A2).

Голые факты для контроллеров (cli/tui): был ли апгрейд, запись стампа.
Презентации здесь нет. Стамп — <gc config dir>/agent_state.json; поле
last_update_check_ts принадлежит update-flow (спека D) и пишется write_state().
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional

from gc_core import app_version
from gc_core.config_modules import env_vars
from gc_core.utils.atomic_io import atomic_replace

STATE_FILENAME = "agent_state.json"


class UpgradeInfo(NamedTuple):
    old_version: Optional[str]
    new_version: str


def _state_path() -> Path:
    return Path(env_vars.effective_config_dir()) / STATE_FILENAME


def read_state(path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path) if path else _state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(updates: Dict[str, Any], path: Optional[Path] = None) -> None:
    p = Path(path) if path else _state_path()
    state = read_state(p)
    state.update(updates)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        atomic_replace(str(tmp), str(p))
    except OSError:
        pass  # стамп — best-effort: недоступный каталог не должен ломать запуск


def _current() -> tuple[str, Optional[str]]:
    src = app_version.install_source()
    return app_version.agent_version(), (src.commit if src else None)


def detect_upgrade(path: Optional[Path] = None) -> Optional[UpgradeInfo]:
    """UpgradeInfo, если версия ИЛИ коммит изменились со стампа; None иначе.

    Стампа нет (первый запуск / первая stamped-версия) → None: это не апгрейд.
    """
    state = read_state(path)
    last_v = state.get("last_run_version")
    last_c = state.get("last_run_commit")
    if last_v is None and last_c is None:
        return None
    cur_v, cur_c = _current()
    if cur_v != last_v or (cur_c or None) != (last_c or None):
        return UpgradeInfo(old_version=last_v, new_version=cur_v)
    return None


def mark_current_run(path: Optional[Path] = None) -> None:
    cur_v, cur_c = _current()
    write_state({"last_run_version": cur_v, "last_run_commit": cur_c}, path)
