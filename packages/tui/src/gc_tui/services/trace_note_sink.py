"""TraceNoteSink — потокобезопасная очередь живых сообщений ленты (TUI).

Кормится `RunContext.on_trace_note` из рабочего потока агента (пометка
«изображение не просмотрено», дизайн картинок §10); тик опроса экрана снимает её
на UI-потоке и монтирует серые строки. Той же очередью идёт событие «выбор
модели применён» (`RunContext.on_model_choice_applied`, спека model-knob
§5.3) — отметка смены модели в ленте. Пометка несёт тред своего прогона: контекст
один на приложение, а сессию можно сменить посреди хода — пометка сессии A в ленту
B не попадает. Слой презентации: без бизнес-логики, не хранилище — в тред пометки
не пишутся."""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Callable, Optional

from gc_core.model_choice import ModelChoiceApplied
from gc_core.thread_paths import thread_address
from gc_core.vision.notes import TraceNote

TraceItem = TraceNote | ModelChoiceApplied


class TraceNoteSink:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._notes: deque[tuple[Optional[str], TraceItem]] = deque()

    def push(self, thread_id: Optional[str], note: TraceItem) -> None:
        with self._lock:
            self._notes.append((thread_id, note))

    def hook_for(self, ctx: Any) -> Callable[[TraceItem], None]:
        """Хук `on_trace_note` контекста: пометка — с тредом прогона на момент вызова
        (`thread_address`; субагент наследует хук и тред родителя)."""

        def hook(note: TraceItem) -> None:
            address = thread_address(ctx)
            self.push(address[1] if address else None, note)

        return hook

    def hook_for_choice(
        self, ctx: Any
    ) -> Callable[[ModelChoiceApplied], None]:
        """Хук `on_model_choice_applied`: событие — с тредом прогона, как
        пометка."""
        return self.hook_for(ctx)

    def drain(self, thread_id: Optional[str]) -> list[TraceItem]:
        """Пометки треда `thread_id` по порядку. Пометки других тредов выбрасываются:
        пометка только живая, а в чужой ленте ей не место."""
        with self._lock:
            notes = [note for tid, note in self._notes if tid == thread_id]
            self._notes.clear()
            return notes
