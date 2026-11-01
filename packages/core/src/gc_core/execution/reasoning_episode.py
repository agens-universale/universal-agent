"""Эпизод мысли главного вызова: текст, время фазы размышления, попытка.

Транзиентный объект одного главного вызова — живёт на ``ctx`` (как часы
хода), не в ``State``: персистентная истина — запись потока ``reasoning``
треда. Текст берётся из готового ответа модели (работает и без потока),
время фазы и метка попытки — из дельт потока: от первой ``think`` до
первой ``answer`` / ``tool`` текущей попытки; ``reset`` (новая попытка
запроса) всё обнуляет и несёт метку попытки дальше по потоку.
"""

from collections.abc import Callable, Mapping
from typing import Any

from gc_core.execution.finish_reason import nonempty_reasoning_text
from gc_core.message_format.serialization import _clip_reasoning
from gc_core.persistence.reasoning_record import ReasoningRecord
from gc_core.utils.convertations import closed_think_texts

_PHASE_END = frozenset({"answer", "tool"})
_ATTR = "reasoning_episode"


def episode_text(message: Mapping) -> str | None:
    """Мысль ответа: ``reasoning_content`` / ``reasoning``, иначе тексты
    закрытых ``<think>…</think>`` в ``content``; пусто — ``None``."""
    text = nonempty_reasoning_text(dict(message))
    if text is None or not text.strip():
        text = "\n".join(closed_think_texts(message.get("content")))
    text = text.strip()
    return _clip_reasoning(text) if text else None


class ReasoningEpisode:
    """Отметки потока и мысль последнего полученного ответа вызова."""

    def __init__(
        self, *, mono: Callable[[], float], new_token: Callable[[], str]
    ) -> None:
        self._mono = mono
        self._new_token = new_token
        self._attempt: str | None = None
        self._started: float | None = None
        self._duration: float | None = None
        self._text: str | None = None

    def observe(self, kind: str, text: str) -> str:
        """Отметить дельту; вернуть текст, который идёт дальше по потоку
        (у ``reset`` — метка новой попытки)."""
        if kind == "reset":
            self._attempt = self._new_token()
            self._started = None
            self._duration = None
            return self._attempt
        if kind == "think" and self._started is None:
            self._started = self._mono()
        elif kind in _PHASE_END:
            self._close_phase()
        return text

    def capture(self, message: Mapping) -> None:
        """Мысль полученного ответа; открытая фаза закрывается им."""
        self._close_phase()
        self._text = episode_text(message)

    def take(self) -> ReasoningRecord | None:
        """Запись эпизода последнего ответа; мысли нет — ``None``."""
        if self._text is None:
            return None
        return ReasoningRecord(
            text=self._text,
            duration_sec=self._duration,
            attempt=self._attempt,
        )

    def _close_phase(self) -> None:
        if self._started is not None and self._duration is None:
            self._duration = self._mono() - self._started


def bind_episode(ctx: Any, episode: ReasoningEpisode | None) -> None:
    """Эпизод текущего главного вызова на ctx (``None`` — снять)."""
    setattr(ctx, _ATTR, episode)


def current_episode(ctx: Any) -> ReasoningEpisode | None:
    """Эпизод текущего главного вызова, если привязан."""
    episode = getattr(ctx, _ATTR, None)
    return episode if isinstance(episode, ReasoningEpisode) else None
