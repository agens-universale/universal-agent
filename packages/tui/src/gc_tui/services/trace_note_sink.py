"""TraceNoteSink — потокобезопасная очередь пометок ленты (TUI).

Кормится `RunContext.on_trace_note` из рабочего потока агента (пометка
«изображение не просмотрено», дизайн картинок §10); тик опроса экрана снимает её
на UI-потоке и монтирует серые строки. Пометка несёт тред своего прогона: контекст
один на приложение, а сессию можно сменить посреди хода — пометка сессии A в ленту
B не попадает. Слой презентации: без бизнес-логики, не хранилище — в тред пометки
не пишутся."""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Callable, Optional

from gc_core.thread_paths import thread_address


class TraceNoteSink:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._notes: deque[tuple[Optional[str], str]] = deque()

    def push(self, thread_id: Optional[str], text: str) -> None:
        with self._lock:
            self._notes.append((thread_id, text))

    def hook_for(self, ctx: Any) -> Callable[[str], None]:
        """Хук `on_trace_note` контекста: пометка — с тредом прогона на момент вызова
        (`thread_address`; субагент наследует хук и тред родителя)."""

        def hook(text: str) -> None:
            address = thread_address(ctx)
            self.push(address[1] if address else None, text)

        return hook

    def drain(self, thread_id: Optional[str]) -> list[str]:
        """Пометки треда `thread_id` по порядку. Пометки других тредов выбрасываются:
        пометка только живая, а в чужой ленте ей не место."""
        with self._lock:
            notes = [text for tid, text in self._notes if tid == thread_id]
            self._notes.clear()
            return notes
