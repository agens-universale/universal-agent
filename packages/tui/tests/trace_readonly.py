"""Read-only рендер плоского списка записей треда — наблюдатель для тестов.

Прод строит превью из сегментов (`trace_render.render_segments_readonly`); тестам
рендера достаточно одного сегмента без маркеров сжатий, поэтому обход записей
живёт здесь, поверх того же `_render_record_readonly`."""

from __future__ import annotations


def render_thread_readonly(mount_target, records: list) -> None:
    """Построить и смонтировать виджеты трейса из плоских записей в mount_target —
    read-only (без pending/баннеров/submit-collapse/индикатора/tool-preview).
    submit_solution-output отфильтрован уже в map_message (→ None)."""
    from gc_tui.services.trace_mapper import collect_render_context
    from gc_tui.services.trace_render import _render_record_readonly

    no = 1
    calls: dict = {}
    context = collect_render_context(
        rec.get("payload") for rec in records if isinstance(rec, dict)
    )
    for rec in records:
        no = _render_record_readonly(mount_target, rec, no, calls, context)
