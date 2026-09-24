"""Фоновый поиск по индексу путей для одного сеанса @-пикера (технический слой:
без Textual, текстов интерфейса и политики показа).

Один поток-демон на сеанс вместо потока на каждое нажатие: нажатие кладёт запрос в
ячейку поверх прежнего и будит поток, поток считает самый свежий. Именно демон, а не
`run_worker(thread=True)`: тот идёт через дефолтный executor цикла событий (общий пул
с буфером обмена, MCP, голосом), и `asyncio.run()` на выходе из `app.run()` дожидался
бы долгой сборки индекса — та же ловушка, что в `TuiApp._start_voice_autostart`."""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from gc_tui.services.file_index import PathIndex

_log = logging.getLogger(__name__)


class FileSearchSession:
    """Поиск одного сеанса пикера: `want` — запрос, `close` — конец сеанса, результат —
    в `on_result(query, entries, truncated)` из потока поиска (`truncated` — сырой
    `index.truncated`, решать про подсказку — экрану). `on_result` не должен бросать:
    исключение из него завершит поток сеанса.

    Главный инвариант: у последнего запроса всегда есть кто-то, кто его посчитает. Его
    держит цикл: `want` пишет ячейку и будит поток под тем же локом, под которым поток
    проверяет её перед сном. Повтор того же запроса считается заново — пропуск «равен
    последнему посчитанному» оставил бы Enter ждать результата, которого никто не
    принесёт (набрали «ab», дописали «c», стёрли до «ab»).

    Лок не держится ни во время сборки и поиска, ни во время `on_result`: колбэк экрана
    ждёт UI-поток, а UI-поток зовёт `want` и `close`."""

    def __init__(
        self,
        index: PathIndex,
        on_result: Callable[[str, list[str], bool], None],
    ) -> None:
        self.index = index
        self._on_result = on_result
        self._cond = threading.Condition()
        self._pending: Optional[str] = None  # ячейка «желаемого»
        self._busy = False  # запрос снят, с ним ещё не закончили
        self._closed = False
        self._exited = False
        threading.Thread(
            target=self._run, name="file-search", daemon=True
        ).start()

    def want(self, query: str) -> None:
        """Посчитать query — поверх ещё не взятого прежнего запроса. Не блокирует."""
        with self._cond:
            self._pending = query
            self._cond.notify_all()

    def close(self) -> None:
        """Конец сеанса. Не блокирует: поток выйдет сам — сразу, если простаивает;
        после сборки, если она идёт (без поиска); после поиска — результат не отдаётся,
        если закрытие успело раньше отдачи (иначе отдаётся, и экран его отбросит)."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        """True — всё, что отдано через `want`, посчитано и отдано в `on_result` (или
        отброшено из-за закрытия), либо поток вышел; False — вышел таймаут. Не из
        UI-потока: поток может ждать его внутри `on_result`."""
        with self._cond:
            return self._cond.wait_for(
                lambda: (
                    (self._pending is None and not self._busy) or self._exited
                ),
                timeout,
            )

    def _run(self) -> None:
        try:
            while True:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()  # ждущим простоя
                    while self._pending is None and not self._closed:
                        self._cond.wait()
                    if self._closed:
                        return
                    query, self._pending = self._pending, None
                    self._busy = True
                try:
                    self.index.build()  # один раз на индекс; дальше мгновенно
                    with self._cond:
                        if self._closed:
                            return  # закрыли во время сборки — не фильтруем
                        if (
                            self._pending is not None
                        ):  # пока собирали, ввод ушёл дальше
                            query, self._pending = self._pending, None
                    entries = self.index.search(query)
                    truncated = self.index.truncated
                except Exception:
                    # Наружу нельзя: умерший поток оставил бы последний запрос без
                    # результата, а Enter — ждать его вечно. Запрос уже снят из ячейки,
                    # так что на один `want` — одна попытка сборки, без горячего цикла.
                    _log.warning(
                        "file search failed: root=%s query=%r",
                        self.index.root,
                        query,
                        exc_info=True,
                    )
                    entries, truncated = [], False
                with self._cond:
                    if self._closed:
                        return  # сеанс закрыт — результат никому не нужен
                self._on_result(query, entries, truncated)
        finally:
            with self._cond:
                self._exited = True
                self._cond.notify_all()
