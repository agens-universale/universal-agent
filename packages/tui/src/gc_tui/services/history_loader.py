"""Фоновая загрузка истории для открытия сессии и превью (спека шага 2
окна ленты, §3.3, §5.7).

Поток читает тред одним снимком и строит модель истории; виджетов и
состояния экрана не трогает — готовое уходит в главный поток через
неблокирующий мост ``post`` (``App.call_later``). Одна задача за раз:
новая отменяет прежнюю, поколение проверяется перед чтением, перед каждой
записью построения и при доставке — результат отменённой выбрасывается.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import NamedTuple

from gc_tui.models import TaskCursor
from gc_tui.services.history_model import (
    HistoryBuildCancelled,
    HistoryInputs,
    HistoryModel,
    build_history,
)
from gc_tui.services.stores.file_store import FileTaskStore
from gc_tui.services.stores.record_view import tui_section
from gc_tui.session_settings import read_from_tui


class LoadedHistory(NamedTuple):
    """Готовая история: модель, курсор хвоста сразу за прочитанным и сами
    сегменты (превью передаёт их экрану сессии без повторного чтения)."""

    model: HistoryModel
    cursor: TaskCursor
    segments: list[dict]


class HistoryLoader:
    """Строит модель истории в фоновом потоке; результат — в ``on_ready``
    на главном потоке, если задачу не отменили и не сменили новой."""

    def __init__(
        self, store, post: Callable[[Callable[[], None]], object]
    ) -> None:
        self._store = store
        self._post = post
        self._generation = 0
        self._busy = False
        # Чтения и построения не идут параллельно: следующая задача ждёт,
        # пока отменённая дойдёт до проверки поколения.
        self._run_lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._busy

    def start(
        self,
        task_id: str,
        inputs: HistoryInputs,
        *,
        segments: list[dict] | None = None,
        cursor: TaskCursor | None = None,
        withdrawn: set[str] | frozenset = frozenset(),
        on_ready: Callable[[LoadedHistory], None],
        on_error: Callable[[Exception], None],
    ) -> int:
        """Начать загрузку; прежняя задача отменяется. ``segments`` и
        ``cursor`` — уже прочитанное (переход из превью): чтения нет.
        Сбой чтения или построения — в ``on_error`` на главном потоке.
        Возвращает поколение задачи."""
        self._generation += 1
        gen = self._generation
        self._busy = True
        job = partial(
            self._run,
            gen,
            task_id,
            inputs,
            segments,
            cursor,
            frozenset(withdrawn),
            on_ready,
            on_error,
        )
        threading.Thread(
            target=job, name="history-loader", daemon=True
        ).start()
        return gen

    def cancel(self) -> None:
        """Отменить текущую задачу: она дорабатывает в фоне, результат
        выбрасывается."""
        self._generation += 1
        self._busy = False

    def _stale(self, gen: int) -> bool:
        return gen != self._generation

    def _run(
        self,
        gen: int,
        task_id: str,
        inputs: HistoryInputs,
        segments: list[dict] | None,
        cursor: TaskCursor | None,
        withdrawn: frozenset,
        on_ready: Callable[[LoadedHistory], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        with self._run_lock:
            if self._stale(gen):
                return
            try:
                if segments is None:
                    segments, cursor = self._store.read_history(task_id)
                model = build_history(
                    segments,
                    inputs=inputs,
                    withdrawn=withdrawn,
                    cancelled=partial(self._stale, gen),
                )
            except HistoryBuildCancelled:
                return
            except Exception as exc:
                self._post(partial(self._fail, gen, exc, on_error))
                return
        loaded = LoadedHistory(model, cursor, segments)
        self._post(partial(self._deliver, gen, loaded, on_ready))

    def _deliver(
        self,
        gen: int,
        loaded: LoadedHistory,
        on_ready: Callable[[LoadedHistory], None],
    ) -> None:
        if self._stale(gen):
            return
        self._busy = False
        on_ready(loaded)

    def _fail(
        self,
        gen: int,
        exc: Exception,
        on_error: Callable[[Exception], None],
    ) -> None:
        """Сбой чтения или построения — в главном потоке; отменённой
        задаче он не нужен."""
        if self._stale(gen):
            return
        self._busy = False
        on_error(exc)


def history_inputs_for(app, task_id: str) -> HistoryInputs:
    """Входы модели для экрана сессии ``task_id`` — до входа в неё, на
    главном потоке: база под профилем этой сессии (служебные поля схем
    тулов задаёт только профиль, сессионный слой их не меняет) и корень —
    папка запуска, как у открытой сессии."""
    store = FileTaskStore(threads_dir=app._threads_dir, system=app._system)
    overrides = read_from_tui(tui_section(store.get_record(task_id)))
    agent = app.session_base_settings_for(overrides).settings.agent
    return HistoryInputs(
        root=str(Path.cwd()),
        injected_fields=tuple(sorted(agent.inject_tool_fields)),
    )
