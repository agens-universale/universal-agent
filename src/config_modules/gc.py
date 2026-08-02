"""Резолвер флага ``gc.force_collect`` — управляет точкой C3 (collect после батча тулов).

C1 (после компакции) и C2 (после close_all в finally) — всегда включены и от флага
не зависят. C3 — условная точка: один ``gc.collect()`` на границе «батч тулов → LLM-шаг».

Разрешение значения:
1. Если в конфиге явно задан ``gc.force_collect`` — использовать его.
2. Иначе: ``True`` для CLI и TUI (долгоживущий / бенчмарк-режим), ``False`` для server
   (``gc.collect()`` глобален для процесса и стопает все треды).

См. ``docs_md/actual/2026-07-29-gc-collect-placement-spec.md``.
"""

from typing import Any, Dict, Optional


def gc_force_collect_enabled(
    config_dict: Optional[Dict[str, Any]],
    *,
    is_tui: bool = False,
    is_cli_mode: bool = False,
) -> bool:
    """Включена ли точка C3 (collect после батча тулов).

    Явный ``gc.force_collect`` в конфиге перекрывает режимный default.
    """
    cfg: Dict[str, Any] = config_dict or {}
    gc_cfg = cfg.get("gc")
    if isinstance(gc_cfg, dict) and "force_collect" in gc_cfg:
        return bool(gc_cfg["force_collect"])
    return is_cli_mode or is_tui
