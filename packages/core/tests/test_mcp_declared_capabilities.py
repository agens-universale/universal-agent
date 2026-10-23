"""Гейт: не обещать серверу того, чего наш stdio-клиент не умеет.

История. В `_INIT_PAYLOAD` стояло
``{"roots": {"listChanged": True}, "sampling": {}}`` — объявление, что клиент
поддерживает оба протокольных расширения. Не поддерживает: клиент
односторонний (пишет запрос → читает ответ с нужным ``id``, остальное
пропускает), обратные запросы сервера обслуживать некому.

Сервер, поверивший обещанию, шлёт ``roots/list`` и ЖДЁТ. Ответа нет — и он
выжидает свой таймаут целиком, прежде чем взять дефолт. Замер на
``@playwright/mcp`` (голый JSON-RPC, один переключатель — capabilities):

    {}                      → первый tools/call 1.1 с
    roots + sampling        → первый tools/call 64.2 с
    только sampling         → первый tools/call 0.7 с
    только roots            → первый tools/call 61.2 с

Симптом выглядел как «браузер медленно стартует»: минута на первое действие,
дальше быстро — сервер спрашивает roots один раз на сессию.

Тест сторожит СВОЙСТВО, а не строку: объявлять можно ровно то, на что у
клиента есть обработчик входящих запросов. Появится реализация — снять
соответствующее имя из ``_UNIMPLEMENTED``, а не править список вслепую.
"""

from gc_core.tools.connect_mcp.transport import _INIT_PAYLOAD

#: Возможности, требующие от КЛИЕНТА отвечать на запросы сервера.
#: Объявлять их можно только вместе с обработчиком входящих запросов.
_UNIMPLEMENTED = ("roots", "sampling", "elicitation")


def test_no_capability_we_cannot_serve():
    declared = _INIT_PAYLOAD["params"]["capabilities"]
    offenders = sorted(set(declared) & set(_UNIMPLEMENTED))
    assert offenders == [], (
        f"объявляем {offenders}, но обслуживать обратные запросы сервера нечем — "
        f"сервер будет ждать нас до своего таймаута на первом же вызове"
    )


def test_every_init_source_declares_the_same():
    """Источник параметров ``initialize`` обязан быть ОДИН.

    Их было два: ``_INIT_PAYLOAD`` (одноразовый stdio и persistent) и
    ``MCP_INIT_PARAMS`` (легаси-http и carrier). Опустошили только первый —
    второй продолжал обещать roots и sampling, и по этому пути сервер по-прежнему
    ждал нас до таймаута. Гейт выше смотрел лишь на ``_INIT_PAYLOAD`` и
    расхождения не видел, поэтому проверяем и совпадение источников.
    """
    from gc_core.tools.connect_mcp.client import MCP_INIT_PARAMS

    assert _INIT_PAYLOAD["params"] == MCP_INIT_PARAMS, (
        "параметры initialize разъехались между локусами"
    )
    offenders = sorted(
        set(MCP_INIT_PARAMS["capabilities"]) & set(_UNIMPLEMENTED)
    )
    assert offenders == [], f"легаси-путь объявляет {offenders}"


def test_client_has_no_incoming_request_handler():
    """Фиксируем причину: обработчика входящих запросов в клиенте нет.

    Если он появится, тест покраснеет и напомнит пересмотреть ``_UNIMPLEMENTED``
    вместе с ``_INIT_PAYLOAD`` — иначе гейт выше начнёт врать в другую сторону.
    """
    from gc_core.tools.connect_mcp import persistent_stdio, transport

    for module in (transport, persistent_stdio):
        src = module.__doc__ or ""
        assert "roots/list" not in src, (
            f"{module.__name__}: похоже, обработчик roots появился — пересмотреть _UNIMPLEMENTED"
        )

    # Разбор строки общий: без id это не ответ на наш запрос, и его
    # пропускают. Входящий запрос сервера (тоже без нашего id) теряется.
    import inspect

    body = inspect.getsource(
        persistent_stdio.PersistentStdioSession._match_line
    )
    assert 'if "id" not in obj' in body and "return False, {}" in body, (
        "_match_line изменился — проверить, не появилась ли обработка "
        "серверных запросов (тогда capabilities можно расширять)"
    )
    reader = inspect.getsource(
        persistent_stdio.PersistentStdioSession._read_matching
    )
    assert "_match_response" in reader and '"method"' not in reader, (
        "_read_matching сам разбирает метод — проверить обработчик "
        "входящих запросов"
    )


def test_protocol_version_still_declared():
    """Пустые capabilities не должны утащить за собой остальной handshake."""
    params = _INIT_PAYLOAD["params"]
    assert params["protocolVersion"]
    assert params["clientInfo"]["name"]
