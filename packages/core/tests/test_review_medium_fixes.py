"""Сторожа средних находок второго ревью.

Один файл на несколько правок: каждая проверяет одно свойство в две-три
строки, и раскладывать их по отдельным файлам значило бы плодить преамбулы.
Блокер и high лежат отдельно, у каждого свой стенд.
"""


# --- M18: что ретраить, а что нести наверх сразу ---------------------------


def test_config_errors_are_not_retryable():
    """Ошибка КОНФИГУРАЦИИ не должна ретраиться и валить алиас модели.

    Здесь стоял голый ``httpx.TransportError``, а он покрывает
    ``UnsupportedProtocol`` (URL без схемы) и ``LocalProtocolError``
    (не-latin1 в заголовке). На такой ошибке предохранитель ставил алиас в
    «недоступна» на всю сессию и перед этим честно ретраил 10/20/40/80 секунд.
    Докстрока предохранителя требовала обратного.
    """
    import httpx
    from gc_core.utils.backend_errors import HTTP_TRANSPORT_ERRORS

    for exc_type in (httpx.UnsupportedProtocol, httpx.LocalProtocolError):
        assert not issubclass(exc_type, HTTP_TRANSPORT_ERRORS), (
            f"{exc_type.__name__} снова считается сетевым сбоем — это ошибка конфигурации, её надо нести наверх сразу"
        )


def test_real_network_failures_are_still_retryable():
    """Обратная половина: настоящие сетевые сбои ретраить по-прежнему надо."""
    import httpx
    from gc_core.utils.backend_errors import HTTP_TRANSPORT_ERRORS

    for exc_type in (
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.ConnectError,
        httpx.ReadError,
        httpx.ProxyError,
        httpx.RemoteProtocolError,
    ):
        assert issubclass(exc_type, HTTP_TRANSPORT_ERRORS), exc_type.__name__


# --- M16: сессия без владельца всё-таки закрывается ------------------------


def test_ownerless_session_arms_an_exit_hook(monkeypatch):
    """Обещание из докстроки должно быть выполнено, а не только записано.

    ``mcp_session_owner`` обещал, что безвладельческие сессии закрываются
    явным ``close_everything()``. Такого вызова не было ни в одном пакете:
    сессия жила до конца процесса, а с ней и Chromium. Сервер вдобавок
    стартует с ``start_new_session=True`` и SIGHUP не получит.
    """
    import atexit

    from gc_core.tools.connect_mcp import persistent_stdio as ps

    registered: list = []
    monkeypatch.setattr(
        atexit, "register", lambda fn, *a, **kw: registered.append(fn)
    )

    registry = ps.PersistentStdioRegistry()
    registry.get_or_create("srv", {"command": "true"})

    assert registered, "хук на выход не поставлен — сессия переживёт процесс"
    assert registered[0] == registry.close_everything


def test_an_owned_session_arms_the_exit_hook_too(monkeypatch):
    """Подстраховка на выходе нужна и владельческой сессии.

    Прежде хук ставился ТОЛЬКО на безвладельческом пути, и на этом же факте
    держалось обоснование «остаток подождёт следующего сноса». Следующего
    сноса у сервера и CLI нет: снос фильтрует по владельцу, а владелец каждый
    раз новый. Значит у снятого прогона остаток не закрывал вообще никто.
    """
    import atexit

    from gc_core.mcp_session_owner import reset_owner, set_owner
    from gc_core.tools.connect_mcp import persistent_stdio as ps

    registered: list = []
    monkeypatch.setattr(
        atexit, "register", lambda fn, *a, **kw: registered.append(fn)
    )

    registry = ps.PersistentStdioRegistry()
    token = set_owner("owner-with-a-run")
    try:
        registry.get_or_create("srv", {"command": "true"})
    finally:
        reset_owner(token)

    assert registered == [registry.close_everything], (
        "у владельческой сессии нет подстраховки на выходе процесса"
    )


def test_exit_hook_is_armed_once(monkeypatch):
    """Хук ставится один раз, а не на каждую сессию."""
    import atexit

    from gc_core.tools.connect_mcp import persistent_stdio as ps

    registered: list = []
    monkeypatch.setattr(
        atexit, "register", lambda fn, *a, **kw: registered.append(fn)
    )

    registry = ps.PersistentStdioRegistry()
    for name in ("a", "b", "c"):
        registry.get_or_create(name, {"command": "true"})

    assert len(registered) == 1, registered


# --- M13: разбор HTML не на loop -------------------------------------------


async def test_simple_fetch_url_parses_html_off_the_loop(monkeypatch):
    """Регексп-разбор — чистый CPU, ему не место на loop вызывающего.

    У ``fetch_url`` он по этой причине давно уходит в поток (замер: 237 мс
    инлайн против 22 мс), у ``simple_fetch_url`` оставался инлайн.
    """
    import json
    import threading

    import httpx

    from gc_core.tools.exec import simple_fetch_url as mod

    loop_thread = threading.get_ident()
    parser_threads = []
    real = mod.SimpleFetchUrl._extract_simple_html_text

    def spy(cls, text):
        parser_threads.append(threading.get_ident())
        return real(text)

    async def request(self, *args, **kwargs):
        return httpx.Response(
            200,
            text="<html><body><p>Worker thread text</p></body></html>",
            headers={"Content-Type": "text/html"},
            request=httpx.Request("GET", "https://example.com/thread-test"),
        ), None

    monkeypatch.setattr(
        mod.SimpleFetchUrl, "_extract_simple_html_text", classmethod(spy)
    )
    monkeypatch.setattr(mod.SimpleFetchUrl, "_arequest_with_retries", request)

    result = json.loads(
        await mod.SimpleFetchUrl().acall("https://example.com/thread-test")
    )
    assert result["ok"] is True
    assert "Worker thread text" in result["content"]
    assert len(parser_threads) == 1
    assert parser_threads[0] != loop_thread, (
        "разбор HTML снова исполняется на loop"
    )


# --- L2: удалённый механизм не должен описываться как действующий ----------


def test_no_portal_left_in_core_sources():
    """В исходниках не должно остаться упоминаний снятого «портала».

    Ветка строит аргументацию на комментариях, и докстрока, описывающая
    удалённый механизм как действующий, дороже обычного мусора: следующий
    читатель будет чинить то, чего нет. Было 61 упоминание в 25 файлах —
    `arun` обещал «синхронный run остался», `_acall_blocking_hook` объяснял
    поход в поток тем, что «LLM-вызовы уходят на портальный loop».
    """
    import pathlib

    import gc_core

    root = pathlib.Path(gc_core.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            if "syncapiinsideeventloop" in low or "портал" in low:
                offenders.append(f"{path.relative_to(root)}:{i}")

    assert offenders == [], (
        "вернулись упоминания снятого портала:\n  " + "\n  ".join(offenders)
    )
