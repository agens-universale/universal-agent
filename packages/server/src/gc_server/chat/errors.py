"""Ошибки запроса: код HTTP и конверт ``error``."""

from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_payload(exc: ApiError) -> dict:
    return {
        "error": {
            "message": exc.message,
            "type": "invalid_request_error",
            "code": exc.code,
        }
    }
