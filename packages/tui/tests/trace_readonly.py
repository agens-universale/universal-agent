"""Отрисовка истории без экрана — наблюдатель для тестов.

Прод рисует историю превью и сессии отрисовщиком кусков
(`history_render.render_turns`) по модели истории; тестам рендера хватает
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


def render_model(mount_target, model, **options):
    """Все ходы модели — в ``mount_target``."""
    from gc_tui.services.history_render import (
        ChunkOptions,
        FeedHost,
        render_turns,
    )

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
