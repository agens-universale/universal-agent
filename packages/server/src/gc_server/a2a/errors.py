"""Коды JSON-RPC фасада. Транспорт только упаковывает их в конверт."""

from __future__ import annotations

from typing import Any

DOMAIN = "a2a-protocol.org"
ERROR_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"
_MISSING = object()


class RpcError(Exception):
    def __init__(
        self,
        code: int,
        reason: str,
        message: str,
        *,
        status: int = 200,
        request_id: Any = _MISSING,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.message = message
        self.status = status
        self.request_id = request_id


class SealError(Exception):
    """Ход кончился, а состояние задачи записать не удалось."""


def error_body(exc: RpcError, request_id: Any) -> dict:
    chosen = request_id if exc.request_id is _MISSING else exc.request_id
    return {
        "jsonrpc": "2.0",
        "id": chosen,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "data": {
                "@type": ERROR_TYPE,
                "reason": exc.reason,
                "domain": DOMAIN,
            },
        },
    }


def parse_error() -> RpcError:
    return RpcError(-32700, "JSONParseError", "Parse error", status=400)


def invalid_request(request_id: Any = None) -> RpcError:
    return RpcError(
        -32600,
        "InvalidRequestError",
        "Invalid Request",
        request_id=request_id,
    )


def method_not_found() -> RpcError:
    return RpcError(-32601, "MethodNotFoundError", "Method not found")


def invalid_params() -> RpcError:
    return RpcError(-32602, "INVALID_PARAMS", "Invalid params")


def internal_error() -> RpcError:
    return RpcError(-32603, "InternalError", "Internal error")


def unauthorized() -> RpcError:
    return RpcError(-32000, "Unauthorized", "Unauthorized", status=401)


def task_not_found() -> RpcError:
    return RpcError(-32001, "TaskNotFoundError", "Task not found")


def not_cancelable() -> RpcError:
    return RpcError(-32002, "TaskNotCancelableError", "Task not cancelable")


def push_not_supported() -> RpcError:
    return RpcError(
        -32003,
        "PushNotificationNotSupportedError",
        "Push notifications are not supported",
    )


def unsupported() -> RpcError:
    return RpcError(
        -32004, "UnsupportedOperationError", "Unsupported operation"
    )


def content_type() -> RpcError:
    return RpcError(
        -32005,
        "ContentTypeNotSupportedError",
        "Content type not supported",
    )


def version_not_supported() -> RpcError:
    return RpcError(
        -32009, "VersionNotSupportedError", "Version not supported"
    )
