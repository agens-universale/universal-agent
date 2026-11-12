"""Read-only рендер плоского списка записей треда — наблюдатель для тестов.

Прод строит превью из сегментов (`trace_render.render_segments_readonly`); тестам
рендера достаточно одного сегмента без маркеров сжатий — его и строит обход
ниже, тем же `render_segments_readonly` (строки вызовов — через модель
блоков, P4)."""

from __future__ import annotations


def render_thread_readonly(mount_target, records: list) -> None:
    """Построить и смонтировать виджеты трейса из плоских записей в mount_target —
    read-only (без pending/баннеров/submit-collapse/индикатора/tool-preview).
    submit_solution-output отфильтрован уже в map_message (→ None)."""
    from gc_tui.services.trace_render import render_segments_readonly

    segment = {"index": 0, "messages": records, "compacted_by": None}
    render_segments_readonly(mount_target, [segment])
