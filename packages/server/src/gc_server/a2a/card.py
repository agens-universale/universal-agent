"""Карточка агента. Имя, описание и текст скилла — секция ``a2a``."""

from __future__ import annotations

from a2a.types import a2a_pb2 as pb

from gc_server.surface import http_origin

CARD_VERSION = "0.4.0"
RPC_PATH = "/rpc"
CARD_PATH = "/.well-known/agent-card.json"


def interface_urls(public_url: str | None, host: str, port: int) -> tuple:
    """``(JSON-RPC, REST)``: ``public_url`` — адрес JSON-RPC (как раньше,
    с ``/rpc`` на конце) или корень, под которым висят оба; пусто — адрес
    сокета."""
    if public_url:
        base = public_url.rstrip("/")
        if base.endswith(RPC_PATH):
            return base, base[: -len(RPC_PATH)]
        return base + RPC_PATH, base
    origin = http_origin(host, port)
    return origin + RPC_PATH, origin


def agent_card(
    *, name: str, description: str, urls: tuple, secured: bool
) -> pb.AgentCard:
    rpc_url, rest_url = urls
    card = pb.AgentCard(
        name=name,
        description=description,
        version=CARD_VERSION,
        supported_interfaces=[
            pb.AgentInterface(
                url=rpc_url, protocol_binding="JSONRPC", protocol_version="1.0"
            ),
            pb.AgentInterface(
                url=rest_url,
                protocol_binding="HTTP+JSON",
                protocol_version="1.0",
            ),
        ],
        capabilities=pb.AgentCapabilities(
            streaming=True, push_notifications=True
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            pb.AgentSkill(
                id="agent",
                name="Агент",
                description=description,
                tags=["agent"],
            )
        ],
    )
    if secured:
        card.security_schemes[
            "bearer"
        ].http_auth_security_scheme.scheme = "Bearer"
        requirement = card.security_requirements.add()
        requirement.schemes["bearer"].SetInParent()
    return card
