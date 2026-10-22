"""Ответ initialize. Версия протокола фасада — 1."""

from __future__ import annotations

import importlib.metadata


def package_version() -> str:
    """Версия дистрибутива gc-acp. Без установки пакета номера нет — «dev»."""
    try:
        return importlib.metadata.version("gc-acp")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


def initialize_result() -> dict:
    return {
        "protocolVersion": 1,
        "agentCapabilities": {
            "loadSession": True,
            "promptCapabilities": {
                "image": False,
                "audio": False,
                "embeddedContext": True,
            },
            "mcpCapabilities": {"http": False, "sse": False},
            "sessionCapabilities": {
                "list": {},
                "resume": {},
                "close": {},
                "delete": {},
            },
        },
        "authMethods": [],
        "agentInfo": {"name": "gc-acp", "version": package_version()},
    }
