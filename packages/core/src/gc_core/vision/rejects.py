"""Потолки картинок — знание процесса о бэкенде (спека §10): сколько картинок модель по
этому адресу принимает за один запрос. Потолок 0 — модель не видит изображений.

Потолок узнаётся по факту: запрос с картинками получил картиночную ошибку
(`utils.backend_errors.is_image_request_error`). Запоминается только явное — отказ от
зрения и названный предел. Выведенное (413, «слишком много» без числа) после
успешного повтора сохраняется отдельно в State для текущего хода и контекста.
Проб и фоновых проверок нет.
Ключ — фактический адрес запроса и id модели: одна и та же модель за разными адресами
(свой vLLM без зрения, OpenRouter со зрением) видит по-разному.

Вне `State` сознательно: это знание процесса о бэкенде, а не состояние запуска —
общее для субагентов и переживает смену треда, как предохранитель модели. Не
персистится: модель, переподнятую со зрением или с другим пределом, узнают
перезапуском агента.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Optional

from gc_core.utils.backend_errors import (
    image_limit_from_error,
    is_image_rejection_error,
)

if TYPE_CHECKING:
    from gc_core.model_router import ResolvedModel

_Key = tuple[str, str]


@dataclass(frozen=True)
class ImageRequestLimit:
    """Успешный потолок своего запроса: адрес, модель, ход, сжатие и живые id.

    Не означает слепоту и не разделяется между запусками или субагентами.
    """

    scope: tuple[str, str, int, int, frozenset[str]]
    cap: int


class ImageRejects:
    """Потокобезопасный реестр потолков `(адрес, модель) → число картинок`.

    Словарь после публикации не меняется: запись под lock собирает новый и
    подменяет ссылку целиком. Поэтому чтение на каждом запросе идёт без блокировки
    и всегда видит целый снимок."""

    def __init__(self, caps: Optional[Mapping[_Key, int]] = None) -> None:
        self._lock = threading.Lock()
        self._caps: Mapping[_Key, int] = dict(caps or {})

    def cap(self, url: str, model: str) -> Optional[int]:
        """Потолок картинок за запрос; None — бэкенд картинки не ограничивал."""
        return self._caps.get((url, model))

    def contains(self, url: str, model: str) -> bool:
        """Модель не видит изображений (потолок 0)."""
        return self.cap(url, model) == 0

    def limit(self, url: str, model: str, cap: int) -> bool:
        """Опустить потолок до `cap`; поднять нельзя. True — потолок стал ниже (или
        появился)."""
        key = (url, model)
        with self._lock:
            current = self._caps.get(key)
            if current is not None and current <= cap:
                return False
            self._caps = {**self._caps, key: cap}
            return True

    def mark(self, url: str, model: str) -> bool:
        """Пометить модель слепой. True — слепой она стала только сейчас (её владелец и
        показывает уведомление); повторная пометка ничего не меняет."""
        return self.limit(url, model, 0)

    def with_limit(self, url: str, model: str, cap: int) -> ImageRejects:
        """Копия реестра с ещё одним потолком — пробный потолок шага, пока успешный
        запрос его не подтвердил. Сам реестр не меняется."""
        view = ImageRejects(self._caps)
        view.limit(url, model, cap)
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


def mark_blind(resolved: ResolvedModel, *, fallback_url: str) -> bool:
    """Пометить модель слепой на процесс — после отказа от зрения
    (`is_image_rejection_error`). Ключ — `request_url` и id модели. True — пометка
    новая: строку «изображение не просмотрено» показывает только её владелец
    (`vision.notes.note_blind`), параллельный повтор молчит."""
    return process_image_rejects().mark(
        request_url(resolved, fallback_url), resolved.model
    )


def learn_image_cap(
    resolved: ResolvedModel, exc: BaseException, *, fallback_url: str
) -> bool:
    """Запомнить на процесс то, что бэкенд сказал о картинках явно: отказ от зрения —
    потолок 0, названный предел («At most N image(s)») — N. Выведенное делением пополам
    (413 — он зависит от объёма истории, «слишком много» без числа) в этот реестр
    не попадает: успешный потолок своего контекста хранит State. True — модель
    только что стала слепой: строку в ленте показывает только этот вызов."""
    if is_image_rejection_error(exc):
        return mark_blind(resolved, fallback_url=fallback_url)
    stated = image_limit_from_error(exc)
    if stated is None:
        return False
    lowered = process_image_rejects().limit(
        request_url(resolved, fallback_url), resolved.model, stated
    )
    return lowered and stated == 0


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
