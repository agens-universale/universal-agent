"""ReasoningClock — аккумулятор ЭПИЗОДА раздумья агента (TUI, Model B).

Питается когнитивными источниками (start/stop по source_id): "stream" (stream-think,
рёберно из поллинга) и когнитивные тул-вызовы (по call_id). Времена копятся в
ТЕКУЩИЙ эпизод (union интервалов — паузы-ожидания в число не идут). `interrupt()`
(действие в мире — не-когнитивный тул) замораживает текущий эпизод как «последний»
и начинает новый с нуля. Эпизод рвётся ТОЛЬКО действием, не паузой.

Два выхода: live() — debounce (min-hold) для живой фразы «размышление» (анти-фликер,
на границы эпизода не влияет); episode() — длительность текущего-или-последнего
эпизода для «обдумал за N».

Презентационный слой: без Textual, без бизнес-логики. Часы инъектируемы (тесты).
"""
from __future__ import annotations

import time

from gc_core.constants.tool_names import PROJECT_MEMORY_TOOL_NAME, READ_STORED_TOOL_OUTPUT_NAME, READ_USER_INTENT_LOG_NAME, SESSION_MEMORY_TOOL_NAME, SESSION_THINK_TOOL_NAME

# Когнитивные тулы — «свой ум» (мысль/память/свой контекст), НЕ действия/внешний мир.
# ev.tool_name = tool_id(wire); эти имена tool_id пропускает как есть → совпадают
# с каноническими из constants.tool_names.
COGNITIVE_TOOLS = frozenset({
    SESSION_THINK_TOOL_NAME,          # session_think — явная мысль
    SESSION_MEMORY_TOOL_NAME,         # session_memory — своя рабочая память
    PROJECT_MEMORY_TOOL_NAME,         # project_memory — своя проектная память
    READ_USER_INTENT_LOG_NAME,        # session_read_user_intent_log — рефлексия интента
    READ_STORED_TOOL_OUTPUT_NAME,     # session_read_stored_tool_output — свой прежний вывод
})


class ReasoningClock:
    def __init__(self, *, clock=time.monotonic, hold: float = 0.5) -> None:
        self._clock = clock
        self._hold = hold
        self._active: set[str] = set()
        self._interval_start: float | None = None
        self._episode: float = 0.0        # накоплено в текущем эпизоде
        self._last_episode: float = 0.0   # длительность последнего завершённого эпизода
        self._last_active_at: float | None = None

    def start(self, source_id: str) -> None:
        if source_id in self._active:
            return
        if not self._active:
            self._interval_start = self._clock()
        self._active.add(source_id)

    def stop(self, source_id: str) -> None:
        if source_id not in self._active:
            return
        self._active.discard(source_id)
        if not self._active:
            self._close_interval()

    def interrupt(self) -> None:
        """Действие в мире: эпизод завершён. Дозакрыть активный интервал, перенести
        текущий эпизод в «последний», начать новый с нуля."""
        self._active.clear()
        self._close_interval()
        if self._episode > 0:
            self._last_episode = self._episode
        self._episode = 0.0

    def reset(self) -> None:
        self._active.clear()
        self._interval_start = None
        self._episode = 0.0
        self._last_episode = 0.0
        self._last_active_at = None

    def live(self) -> bool:
        if self._active:
            return True
        if self._last_active_at is None:
            return False
        return (self._clock() - self._last_active_at) < self._hold

    def episode(self) -> float:
        """Длительность текущего-или-последнего эпизода (НЕ сумма за такт)."""
        cur = self._episode
        if self._active and self._interval_start is not None:
            cur += self._clock() - self._interval_start
        if cur > 0:
            return cur
        return self._last_episode

    def _close_interval(self) -> None:
        """Закрыть открытый интервал в текущий эпизод (если был активен)."""
        if self._interval_start is not None:
            now = self._clock()
            self._episode += now - self._interval_start
            self._last_active_at = now
            self._interval_start = None
