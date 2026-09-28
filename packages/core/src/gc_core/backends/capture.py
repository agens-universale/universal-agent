"""Bounded head+tail capture одного потока вывода процесса.

``L`` символов на поток делятся на ``H = floor(L / 2)`` головы и ``T = L - H``
хвоста. Голова неизменяема после заполнения, хвост — кольцевой буфер чанков
(``deque``) с отдельным счётчиком символов: старые символы вытесняются слева.
Полный поток в памяти не накапливается никогда, время линейно по объёму
реально прочитанного вывода, память — ``O(L)`` на поток.

Реализация опирается только на стандартную библиотеку: ни ``head``, ни
``tail``, ни ``tee``, ни GNU coreutils в исполняемой среде не требуются.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CapturedStream:
    """Наблюдённый поток: настоящие края + точные счётчики.

    ``truncated=False`` — ``head + tail`` есть весь поток целиком (середина не
    отбрасывалась). ``truncated=True`` — между ``head`` и ``tail`` необратимо
    потеряно ``omitted_chars`` символов.
    """

    head: str = ""
    tail: str = ""
    total_chars: int = 0
    truncated: bool = False

    @property
    def omitted_chars(self) -> int:
        return self.total_chars - len(self.head) - len(self.tail)

    @property
    def content(self) -> str:
        """Полный поток; осмысленно только при ``truncated=False``."""
        return self.head + self.tail

    @property
    def text(self) -> str:
        """Наблюдённый текст без синтетического маркера между краями.

        Для внутренних потребителей backend (grep/find/walk), которым нужна
        плоская строка, а не структура.
        """
        return self.head + self.tail


@dataclass
class BoundedStreamAccumulator:
    """Инкрементальный сборщик ``CapturedStream`` с ограниченной памятью."""

    capture_max_chars: int
    _head: list = field(default_factory=list, init=False)
    _head_chars: int = field(default=0, init=False)
    _tail: deque = field(default_factory=deque, init=False)
    _tail_chars: int = field(default=0, init=False)
    _total_chars: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        cap = self.capture_max_chars
        if not isinstance(cap, int) or cap < 0:
            cap = 0
        self.capture_max_chars = cap
        self._head_max = cap // 2
        self._tail_max = cap - cap // 2

    def feed(self, text: str) -> None:
        if not text:
            return
        self._total_chars += len(text)
        if self.capture_max_chars == 0:
            return
        room = self._head_max - self._head_chars
        if room > 0:
            piece = text[:room]
            self._head.append(piece)
            self._head_chars += len(piece)
            text = text[room:]
            if not text:
                return
        # Кольцевой хвост: чанк целиком в deque, лишнее вытесняем слева.
        # Никакого `(tail + chunk)[-T:]` — это копировало бы хвост на каждый чанк.
        if len(text) >= self._tail_max:
            self._tail.clear()
            self._tail.append(text[len(text) - self._tail_max :])
            self._tail_chars = self._tail_max
            return
        self._tail.append(text)
        self._tail_chars += len(text)
        while self._tail_chars > self._tail_max:
            overflow = self._tail_chars - self._tail_max
            oldest = self._tail[0]
            if len(oldest) <= overflow:
                self._tail.popleft()
                self._tail_chars -= len(oldest)
            else:
                self._tail[0] = oldest[overflow:]
                self._tail_chars -= overflow

    def result(self) -> CapturedStream:
        return CapturedStream(
            head="".join(self._head),
            tail="".join(self._tail),
            total_chars=self._total_chars,
            truncated=self._total_chars > self.capture_max_chars,
        )


def captured_stream_from_text(
    text: str, capture_max_chars: int
) -> CapturedStream:
    """Нормализовать уже готовый полный поток тем же head+tail алгоритмом.

    Для путей, где вывод пришёл целиком (Harbor без streaming callback): внешний
    контракт совпадает, но bounded RAM внутри чужой реализации не гарантируется.
    """
    acc = BoundedStreamAccumulator(capture_max_chars)
    acc.feed(text or "")
    return acc.result()
