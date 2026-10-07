"""Выбор эффективного транспорта: persistent_session и депрекейт sse.

Правило одно на два вызывающих (``api.call_mcp_tool`` и
``registry._discover_one``) — раньше оно было продублировано и разъезжалось.
"""

import logging

import pytest
from gc_core.tools.connect_mcp import transport_policy
from gc_core.tools.connect_mcp.transport_policy import (
    resolve_effective_transport,
)


@pytest.fixture(autouse=True)
def _forget_warnings():
    """Предупреждение печатается раз на сервер — между тестами забываем."""
    transport_policy._warned.clear()
    yield
    transport_policy._warned.clear()


def test_sse_is_deprecated_and_routed_to_sdk():
    """`sse` всегда уезжает в stream_sse: своя реализация не по спеке."""
    assert resolve_effective_transport({"transport": "sse"}) == "stream_sse"


def test_sse_deprecation_warns_once_per_server(caplog):
    cfg = {"transport": "sse"}
    with caplog.at_level(logging.WARNING):
        resolve_effective_transport(cfg, server_name="srv")
        resolve_effective_transport(cfg, server_name="srv")

    warnings = [r for r in caplog.records if "deprecated" in r.getMessage()]
    assert len(warnings) == 1, "предупреждение должно быть одно на сервер"
    assert "stream_sse" in warnings[0].getMessage()


def test_other_transports_pass_through():
    """Всё, кроме sse, остаётся как заявлено — депрекейт точечный."""
    for name in ("http", "stdio", "stream_sse", "stream_http", "stream_stdio"):
        assert resolve_effective_transport({"transport": name}) == name


def test_persistent_session_routes_http_to_sdk():
    """Правило persistent_session сохранено: у легаси нет своего пула."""
    assert (
        resolve_effective_transport(
            {"transport": "http", "persistent_session": True}
        )
        == "stream_http"
    )


def test_persistent_session_does_not_break_stdio():
    assert (
        resolve_effective_transport(
            {"transport": "stdio", "persistent_session": True}
        )
        == "stdio"
    )


def test_both_callers_use_the_same_helper():
    """Гейт против повторного расхождения: своей копии правила быть не должно.

    Резолв зовёт `api`; `registry` получает готовый `effective_transport`
    параметром и хелпер не вызывает — его имя там держалось на НЕиспользуемом
    импорте, то есть проверка «имя есть в исходнике» для него была холостой
    (снят вместе с остальными F401). Поэтому наличие вызова спрашиваем у того,
    кто резолвит, а у обоих — отсутствие своей копии правила.
    """
    import inspect

    from gc_core.tools.connect_mcp import api, registry

    assert "resolve_effective_transport(" in inspect.getsource(api)
    for mod in (api, registry):
        src = inspect.getsource(mod)
        assert 'effective_transport = "stream_sse"' not in src, (
            f"{mod.__name__}: правило снова продублировано"
        )
