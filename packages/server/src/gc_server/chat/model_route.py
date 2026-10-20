"""Список id и развилка ``model`` процесса gc-chat."""

from __future__ import annotations

AGENT_ID = "GC-UA"

_OWNED_BY = "gc-chat"


def models_body(config_model: str) -> dict:
    """``GC-UA`` первым, затем непустой id модели из конфига."""
    ids = [AGENT_ID]
    if config_model != "":
        ids.append(config_model)
    return {"object": "list", "data": [_model(model_id) for model_id in ids]}


def reserved_model_reason(config_model: str) -> str | None:
    """Совпадение с ``GC-UA`` — процесс не должен открывать сокет."""
    if config_model != AGENT_ID:
        return None
    return "gc-chat: модель конфига GC-UA совпадает с id агента"


def completions_route(sent: object, config_model: str) -> str | None:
    """``agent``, ``direct`` или ``None``, если id чужой.

    Сравнение точное: без приведения регистра и без обрезки пробелов.
    """
    if sent == AGENT_ID:
        return "agent"
    if config_model != "" and sent == config_model:
        return "direct"
    return None


def _model(model_id: str) -> dict:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": _OWNED_BY,
    }
