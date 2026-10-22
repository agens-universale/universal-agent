"""Карточка агента. Имя, описание и текст скилла — секция ``a2a``."""

from __future__ import annotations

CARD_VERSION = "0.4.0"


def http_origin(host: str, port: int) -> str:
    shown = host
    if ":" in host and not host.startswith("["):
        shown = "[" + host + "]"
    return "http://" + shown + ":" + str(port)


def interface_url(host: str, port: int) -> str:
    return http_origin(host, port) + "/rpc"


def agent_card(
    *, name: str, description: str, url: str, secured: bool
) -> dict:
    card = {
        "name": name,
        "description": description,
        "supportedInterfaces": [
            {
                "url": url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "version": CARD_VERSION,
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "agent",
                "name": "Агент",
                "description": description,
                "tags": ["agent"],
            }
        ],
    }
    if secured:
        card["securitySchemes"] = {
            "bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}
        }
        card["securityRequirements"] = [{"schemes": {"bearer": {}}}]
    return card
