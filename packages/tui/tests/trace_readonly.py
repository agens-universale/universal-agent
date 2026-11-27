"""Отрисовка истории без экрана — наблюдатель для тестов.

Прод рисует историю превью и сессии отрисовщиком кусков
(`history_render.render_steps`) по модели истории; тестам рендера хватает
модели из сегментов (или из готовых событий) и всех её ходов разом."""

from __future__ import annotations


def render_segments_history(
    mount_target,
    segments: list,
    *,
    root: str | None = None,
    injected_fields=(),
    **options,
):
    """Модель из сегментов — вся в ``mount_target``; ``options`` — поля
    `ChunkOptions`. Возвращает `ChunkRefs`."""
    from gc_tui.services.history_model import HistoryInputs, build_history

    model = build_history(
        [{"compacted_by": None, **seg} for seg in segments],
        inputs=HistoryInputs(
            root=root, injected_fields=tuple(injected_fields)
        ),
    )
    return render_model(mount_target, model, **options)


def render_turns(host, model, turns: range, **kwargs):
    """Ходы ``turns`` модели целиком — все их шаги (`render_steps`)."""
    from gc_tui.services.history_render import render_steps

    steps = range(model.turn_step(turns.start), model.turn_step(turns.stop))
    return render_steps(host, model, steps, **kwargs)


def render_model(mount_target, model, **options):
    """Все ходы модели — в ``mount_target``."""
    from gc_tui.services.history_render import ChunkOptions, FeedHost

    return render_turns(
        FeedHost(mount_target),
        model,
        range(model.turn_count()),
        before=None,
        options=ChunkOptions(**options),
    )


def model_of(*events):
    """Модель истории из готовых событий одним ходом."""
    from gc_tui.services.history_model import HistoryModel

    model = HistoryModel()
    model.turn_starts.append(0)
    for ev in events:
        model.append_event(ev)
    return model


def render_events(mount_target, *events, **options):
    """События одним ходом — в ``mount_target``."""
    return render_model(mount_target, model_of(*events), **options)


def render_thread_readonly(mount_target, records: list) -> None:
    """Плоские записи треда одним сегментом — в ``mount_target``."""
    render_segments_history(mount_target, [{"index": 0, "messages": records}])


def merge_nested(
    mount_target,
    records: list,
    calls: dict,
    *,
    allowed_parent_ids: set[str] | None = None,
    blocks=None,
    root: str | None = None,
    mcp_tools=None,
) -> None:
    """Вложенный трейс записей субагентов — под строки ``calls`` узлом
    вложенного трейса; ``allowed_parent_ids`` — родители окна удержания
    (None — все из ``calls``), ``blocks`` — блоки превью."""
    from gc_tui.services.history_render import ChunkOptions, FeedHost
    from gc_tui.services.nested_trace import NestedSource
    from gc_tui.widgets.tool_blocks_view import PreviewBlocks

    blocks = blocks if blocks is not None else PreviewBlocks()
    window = set(calls) if allowed_parent_ids is None else allowed_parent_ids
    node = NestedSource(
        lambda: records,
        window_parent_ids=lambda: window,
        root=root,
        injected=(),
    )
    node.mount(
        FeedHost(mount_target),
        node.actions_for(set(calls)),
        calls,
        views=blocks.views,
        options=ChunkOptions(
            edits_lookup=blocks.edits_lookup,
            diff_preview=blocks.diff_preview,
            expanded_turns=blocks.expanded_turns,
            mcp_tools=mcp_tools,
            timers=blocks.timers,
        ),
    )
