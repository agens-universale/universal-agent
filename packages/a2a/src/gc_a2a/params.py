"""Разбор параметров методов. О задачах не решает: только форма входа."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gc_a2a.errors import content_type, invalid_params, push_not_supported

_CONTENT = ("text", "raw", "url", "data")
_PLAIN = "text/plain"
_STATES = frozenset(
    {
        "TASK_STATE_UNSPECIFIED",
        "TASK_STATE_WORKING",
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_REJECTED",
        "TASK_STATE_AUTH_REQUIRED",
    }
)


class Incoming:
    def __init__(
        self,
        *,
        message_id: str,
        parts: tuple[str, ...],
        context_id: str | None,
        task_id: str | None,
        cwd_sent: str | None,
        return_immediately: bool,
        history_length: int | None,
    ) -> None:
        self.message_id = message_id
        self.parts = parts
        self.context_id = context_id
        self.task_id = task_id
        self.cwd_sent = cwd_sent
        self.return_immediately = return_immediately
        self.history_length = history_length

    @property
    def text(self) -> str:
        return "\n".join(self.parts)


class TaskQuery:
    def __init__(
        self,
        *,
        context_id: str | None,
        status: str | None,
        after: datetime | None,
        history_length: int | None,
        page_size: int,
        cursor: tuple[str, int] | None,
        include_artifacts: bool,
    ) -> None:
        self.context_id = context_id
        self.status = status
        self.after = after
        self.history_length = history_length
        self.page_size = page_size
        self.cursor = cursor
        self.include_artifacts = include_artifacts


def parse_send(params: Any) -> Incoming:
    body = _object(params)
    _reject_push(body.get("configuration"))
    _reject_tenant(body.get("tenant"))
    config = _configuration(body)
    _reject_output_modes(config)
    message = body.get("message")
    if not isinstance(message, dict):
        raise invalid_params()
    _require_role(message.get("role"))
    return Incoming(
        message_id=_required_id(message.get("messageId")),
        parts=_parts(message.get("parts")),
        context_id=_optional_id(message, "contextId"),
        task_id=_optional_id(message, "taskId"),
        cwd_sent=_cwd(message.get("metadata")),
        return_immediately=_flag(config, "returnImmediately"),
        history_length=_history_length(config, "historyLength"),
    )


def parse_task_id(params: Any) -> str:
    return _required_id(_object(params).get("id"))


def parse_get(params: Any) -> tuple[str, int | None]:
    body = _object(params)
    return _required_id(body.get("id")), _history_length(body, "historyLength")


def parse_list(params: Any) -> TaskQuery:
    body = _object_or_empty(params)
    return TaskQuery(
        context_id=_optional_id(body, "contextId"),
        status=_status(body.get("status")),
        after=_after(body.get("statusTimestampAfter")),
        history_length=_history_length(body, "historyLength"),
        page_size=_page_size(body.get("pageSize")),
        cursor=_cursor(body),
        include_artifacts=_include_artifacts(body.get("includeArtifacts")),
    )


def _object(params: Any) -> dict:
    if not isinstance(params, dict):
        raise invalid_params()
    return params


def _object_or_empty(params: Any) -> dict:
    if params is None:
        return {}
    return _object(params)


def _configuration(body: dict) -> dict | None:
    if "configuration" not in body or body["configuration"] is None:
        return None
    config = body["configuration"]
    if not isinstance(config, dict):
        raise invalid_params()
    return config


def _reject_push(config: Any) -> None:
    if not isinstance(config, dict):
        return
    if config.get("taskPushNotificationConfig") is not None:
        raise push_not_supported()


def _reject_tenant(value: Any) -> None:
    if value not in (None, ""):
        raise invalid_params()


def _reject_output_modes(config: dict | None) -> None:
    if config is None or "acceptedOutputModes" not in config:
        return
    modes = config["acceptedOutputModes"]
    if modes is None:
        return
    if not isinstance(modes, list) or any(
        not isinstance(item, str) for item in modes
    ):
        raise invalid_params()
    if _PLAIN not in modes:
        raise content_type()


def _require_role(role: Any) -> None:
    if role != "ROLE_USER":
        raise invalid_params()


def _required_id(value: Any) -> str:
    if not isinstance(value, str) or value == "":
        raise invalid_params()
    return value


def _optional_id(body: dict, key: str) -> str | None:
    if key not in body or body[key] is None:
        return None
    return _required_id(body[key])


def _parts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise invalid_params()
    return tuple(_part(item) for item in value)


def _part(item: Any) -> str:
    if not isinstance(item, dict):
        raise invalid_params()
    present = [key for key in _CONTENT if key in item]
    if present != ["text"]:
        raise content_type()
    text = item.get("text")
    if not isinstance(text, str):
        raise invalid_params()
    _media(item.get("mediaType"))
    return text


def _media(value: Any) -> None:
    if value in (None, "", _PLAIN):
        return
    raise content_type()


def _cwd(metadata: Any) -> str | None:
    if not isinstance(metadata, dict):
        return None
    cwd = metadata.get("cwd")
    if not isinstance(cwd, str) or cwd == "":
        return None
    return cwd


def _flag(config: dict | None, key: str) -> bool:
    if config is None or key not in config or config[key] is None:
        return False
    value = config[key]
    if not isinstance(value, bool):
        raise invalid_params()
    return value


def _history_length(body: dict | None, key: str) -> int | None:
    if body is None or key not in body or body[key] is None:
        return None
    return _bounded_int(body[key], minimum=0, maximum=None)


def _page_size(value: Any) -> int:
    if value is None:
        return 50
    return _bounded_int(value, minimum=1, maximum=100)


def _bounded_int(value: Any, *, minimum: int, maximum: int | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise invalid_params()
    if value < minimum or (maximum is not None and value > maximum):
        raise invalid_params()
    return value


def _cursor(body: dict) -> tuple[str, int] | None:
    token = body.get("pageToken")
    if token is None:
        token = body.get("nextPageToken")
    if token in (None, ""):
        return None
    if not isinstance(token, str):
        raise invalid_params()
    stamp, sep, sequence = token.rpartition("|")
    if sep == "" or stamp == "" or not sequence.isdigit():
        raise invalid_params()
    return stamp, int(sequence)


def _include_artifacts(value: Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise invalid_params()
    return value


def _status(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _STATES:
        raise invalid_params()
    return value


def _after(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise invalid_params()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise invalid_params() from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
