"""Numeric image limits for effective routes; support lives in registry."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Optional

from gc_core.utils.backend_errors import (
    image_limit_from_error,
    is_image_rejection_error,
)
from gc_core.vision.registry import process_vision_probes
from gc_core.vision.route import VisionRouteKey

if TYPE_CHECKING:
    from gc_core.model_router import ResolvedModel

_Key = VisionRouteKey


@dataclass(frozen=True)
class ImageRequestLimit:
    """Успешный потолок своего запроса: адрес, модель, ход, сжатие и живые id.

    Не означает слепоту и не разделяется между запусками или субагентами.
    """

    scope: tuple[VisionRouteKey, str, int, int, frozenset[str]]
    cap: int


class ImageRejects:
    """Потокобезопасный реестр потолков `(адрес, модель) → число картинок`.

    Словарь после публикации не меняется: запись под lock собирает новый и
    подменяет ссылку целиком. Поэтому чтение на каждом запросе идёт без блокировки
    и всегда видит целый снимок."""

    def __init__(self, caps: Optional[Mapping[_Key, int]] = None) -> None:
        self._lock = threading.Lock()
        self._caps: Mapping[_Key, int] = dict(caps or {})

    def cap(self, key: VisionRouteKey) -> Optional[int]:
        """Numeric limit; zero is not proof that a route is blind."""
        return self._caps.get(key)

    def limit(self, key: VisionRouteKey, cap: int) -> bool:
        with self._lock:
            current = self._caps.get(key)
            if current is not None and current <= cap:
                return False
            self._caps = {**self._caps, key: cap}
            return True

    def with_limit(self, key: VisionRouteKey, cap: int) -> ImageRejects:
        view = ImageRejects(self._caps)
        view.limit(key, cap)
        return view

    def _clear(self) -> None:
        with self._lock:
            self._caps = {}


_PROCESS_REJECTS = ImageRejects()


def process_image_rejects() -> ImageRejects:
    """Единственный реестр потолков процесса."""
    return _PROCESS_REJECTS


def request_url(resolved: ResolvedModel, fallback_url: str) -> str:
    """Фактический адрес запроса модели — ключ потолка. Запись реестра без своего
    адреса и без общего `completions_url` уходит на запасной адрес воронки
    (`processing_responses`, `utils/llm_chat_client`) — он же и ключ."""
    return resolved.url or fallback_url


def mark_blind(key: VisionRouteKey) -> bool:
    return process_vision_probes().mark_unsupported(
        key, reason="image_unsupported"
    )


def learn_image_cap(key: VisionRouteKey, exc: BaseException) -> bool:
    """Explicit support refusals and numeric limits have separate registries."""
    if is_image_rejection_error(exc):
        return mark_blind(key)
    stated = image_limit_from_error(exc)
    if stated is not None:
        process_image_rejects().limit(key, stated)
        if stated == 0:
            return mark_blind(key)
    return False


def next_image_cap(exc: BaseException, sent: int) -> int:
    """Сколько картинок слать в повторе после картиночной ошибки, если в запросе их было
    `sent`: отказ от зрения — 0; предел назван в тексте — он; иначе — вдвое меньше.
    Всегда меньше `sent`: цепочка повторов конечна."""
    if is_image_rejection_error(exc):
        return 0
    stated = image_limit_from_error(exc)
    cap = sent // 2 if stated is None else stated
    return max(0, min(cap, sent - 1))


def reset_image_rejects_for_tests() -> None:
    """Сбросить потолки процесса на месте — объект тот же: ссылки, взятые до сброса,
    видят пустой реестр. Зовёт autouse-фикстура набора ядра."""
    _PROCESS_REJECTS._clear()
