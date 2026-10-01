"""«Кто смотрит» изображения одного запроса (спека изображений §9.1).

Одно решение на запрос — из него читают все: проекция (живые картинки или
записки), фильтр тулов (`tools_for_request`: виден ли `fs_view_image`) и сам
тул (кандидаты).
Считает контроллер (`processing_responses`) перед каждым запросом, передаёт
значением и кладёт в `state.vision_audience` для тула. Состояния здесь нет:
пометки слепоты — в `vision.rejects`, модели — у Router'а, заявленная слепота
(`backend.vision: false`) — в конфиге.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from gc_core.constants.tool_names import FS_VIEW_IMAGE_NAME
from gc_core.model_router import (
    AliasCircuitOpenError,
    ModelRouter,
    ResolvedModel,
)
from gc_core.utils.connect_backend import (
    NO_BACKEND_OVERRIDE,
    backend_override_value,
)
from gc_core.vision.rejects import ImageRejects, request_url


@dataclass(frozen=True)
class Audience:
    """`native` — модель запроса, если она увидит картинку сама; `eyes` —
    модель для зрения, если на неё можно рассчитывать; `tool_visible` — тул
    `fs_view_image` в наборе запроса и смотреть есть кому."""

    native: ResolvedModel | None
    eyes: ResolvedModel | None
    tool_visible: bool

    @property
    def nobody(self) -> bool:
        return self.native is None and self.eyes is None


def _declared_blind(
    resolved: ResolvedModel, config: Mapping[str, Any]
) -> bool:
    """Модель заявлена слепой в конфиге: `backend.vision: false` у неё
    (`models.<alias>.backend`) или, без своего значения, в общей секции
    (плоский `backend_vision`). Для прокси, что молча выбрасывают картинку: отказа
    нет — узнать по факту нечем. Смысл имеет только `false`."""
    value = backend_override_value(resolved, "vision")
    if value is NO_BACKEND_OVERRIDE:
        value = config.get("backend_vision")
    return value is False


def _resolve_eyes(router: ModelRouter) -> ResolvedModel | None:
    """Модель для зрения или None, если резолв отказал: открытый алиас
    (`AliasCircuitOpenError` — вызов зрения ушёл бы в откат, а не к ним) или
    нерезолвимый уровень (`ValueError`)."""
    try:
        return router.resolve("vision")
    except (AliasCircuitOpenError, ValueError):
        return None


def compute_audience(
    router: ModelRouter,
    rejects: ImageRejects,
    *,
    request_resolved: ResolvedModel,
    fallback_url: str,
    api_mode: str,
    tool_in_set: bool,
    config: Mapping[str, Any],
) -> Audience:
    """`native` — модель запроса, только в `chat_completions` (в `responses`
    картинка осталась бы в цепочке `previous_response_id`, §20.5) и только если
    она видит: не помечена после отказа и не заявлена слепой в конфиге
    (`config` — плоский `config_dict`). `eyes` — `router.resolve("vision")` при
    том же условии: неназначенные «глаза» — та же активная модель, и её
    слепота снимает их вместе с ней.

    Зовётся на КАЖДЫЙ запрос и потому не бросает: резолв «глаз» отказал (алиас
    открыт в предохранителе, уровень не резолвится) — на «глаза» не
    рассчитываем, запрос уходит как обычно. Контекст без Router'а сюда не
    доходит: смотреть там некому, это решает контроллер."""

    def sees(resolved: ResolvedModel) -> bool:
        return not _declared_blind(resolved, config) and not rejects.contains(
            request_url(resolved, fallback_url), resolved.model
        )

    native = None
    if api_mode == "chat_completions" and sees(request_resolved):
        native = request_resolved
    candidate = _resolve_eyes(router)
    eyes = candidate if candidate is not None and sees(candidate) else None
    return Audience(
        native=native,
        eyes=eyes,
        tool_visible=tool_in_set and (native is not None or eyes is not None),
    )


def view_tool_in_set(tools: Sequence[Mapping[str, Any]]) -> bool:
    """В наборе тулов запуска есть ``fs_view_image``. Набор уже отфильтрован при
    сборке (``stop_tools``, списки тулов субагента) — записки не отправят модель к
    отсутствующему тулу (спека §9.1)."""
    return any(tool.get("name") == FS_VIEW_IMAGE_NAME for tool in tools)


def tools_for_request(tools: list[dict], audience: Audience) -> list[dict]:
    """Набор тулов на запрос (спека §9.2): ``fs_view_image`` виден, только когда
    есть кому смотреть (``audience.tool_visible``). Скрытый тул диспетчер всё
    равно исполнит (``ctx.tools_data``) — он ответит ``ok:false``, а не
    ``unknown_tool``."""
    if audience.tool_visible:
        return list(tools)
    return [tool for tool in tools if tool.get("name") != FS_VIEW_IMAGE_NAME]
