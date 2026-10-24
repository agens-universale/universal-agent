"""Pure image admission and lazy tool availability for a prepared route."""

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
from gc_core.vision.probe import ProbeStatus
from gc_core.vision.registry import process_vision_probes


@dataclass(frozen=True)
class Audience:
    """`native` — модель запроса, если она увидит картинку сама; `eyes` —
    модель для зрения, если на неё можно рассчитывать; `tool_visible` — тул
    `fs_view_image` в наборе запроса и смотреть есть кому."""

    native: ResolvedModel | None
    eyes: ResolvedModel | None
    tool_visible: bool
    probe_reason: str | None = None

    @property
    def nobody(self) -> bool:
        return self.native is None and self.eyes is None


def _declared_blind(resolved: ResolvedModel, settings: Any) -> bool:
    """Модель заявлена слепой в конфиге: `backend.vision: false` у неё
    (`models.<alias>.backend`) или, без своего значения, в общей секции
    `backend`. True и None допускают автоматическую пробу.

    ``settings`` — всё, что принимает ``settings_of``."""
    from gc_core.settings import settings_of

    value = backend_override_value(resolved, "vision")
    if value is NO_BACKEND_OVERRIDE:
        value = settings_of(settings).backend.vision
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
    *,
    request_resolved: ResolvedModel,
    api_mode: str,
    tool_in_set: bool,
    settings: Any,
    routes: Mapping | None = None,
    turn_key: str | None = None,
) -> Audience:
    """Only confirmed routes admit pixels; unknown eligible routes offer the tool.

    Numeric limits do not establish blindness. Network operations happen in
    vision.service; this function only reads route keys and cached results.
    """
    routes = routes or {}

    def result_for(resolved):
        key = routes.get(route_id(resolved))
        return process_vision_probes().peek(key) if key is not None else None

    def sees(resolved):
        result = result_for(resolved)
        return (
            not _declared_blind(resolved, settings)
            and result is not None
            and result.status is ProbeStatus.CONFIRMED
        )

    def eligible(resolved):
        if resolved is None or _declared_blind(resolved, settings):
            return False
        result = result_for(resolved)
        return (
            result is None
            or result.status is ProbeStatus.CONFIRMED
            or (
                result.status is ProbeStatus.TRANSIENT
                and process_vision_probes().can_probe(
                    routes[route_id(resolved)], turn_key=turn_key
                )
            )
        )

    native = (
        request_resolved
        if api_mode == "chat_completions" and sees(request_resolved)
        else None
    )
    candidate = _resolve_eyes(router)
    eyes = candidate if candidate is not None and sees(candidate) else None
    result = result_for(request_resolved)
    if result is None and candidate is not None:
        result = result_for(candidate)
    return Audience(
        native=native,
        eyes=eyes,
        tool_visible=tool_in_set
        and (
            eligible(candidate)
            or (api_mode == "chat_completions" and eligible(request_resolved))
        ),
        probe_reason=result.reason
        if result is not None and result.status is not ProbeStatus.CONFIRMED
        else None,
    )


def viewing_eyes(
    ctx: Any, state: Any, blind: ResolvedModel
) -> ResolvedModel | None:
    """Модель для зрения, которой агент посмотрит картинку за модель
    ``blind``, или None: тул ``fs_view_image`` в наборе запуска, модель
    зрения назначена другим маршрутом, не заявлена слепой и её проба не
    провалена (нет результата, подтверждена или временный сбой)."""
    router = getattr(ctx, "model_router", None)
    tools = getattr(state, "tools", None) or []
    if router is None or not view_tool_in_set(tools):
        return None
    eyes = _resolve_eyes(router)
    if (
        eyes is None
        or route_id(eyes) == route_id(blind)
        or _declared_blind(eyes, ctx)
    ):
        return None
    routes = getattr(state, "vision_routes", None) or {}
    key = routes.get(route_id(eyes))
    result = process_vision_probes().peek(key) if key is not None else None
    if result is None or result.status in _EYES_USABLE:
        return eyes
    return None


_EYES_USABLE = (ProbeStatus.CONFIRMED, ProbeStatus.TRANSIENT)


def route_id(resolved):
    return resolved.alias, resolved.model, resolved.url


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
