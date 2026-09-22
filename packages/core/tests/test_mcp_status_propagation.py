from gc_core import tools as tools_pkg


async def test_get_mcp_tools_data_returns_status(monkeypatch):
    # Подменяем ТОТ builder, который зовёт код. Раньше здесь стоял
    # синхронный `_build_tools_registry`: подмена била мимо, а тест зеленел на
    # настоящей дискавери — `office-mcp` и правда не установлен. Такая
    # проверка ничего не сторожила.
    async def fake_build(servers_cfg, *, discovery_timeout_sec=8, on_progress=None,
                         os_backend=None):
        return ({}, {"office": {"status": "failed", "reason": "сервер не отозвался",
                                "tool_count": 0, "elapsed_ms": 3}})
    # Патчим по ГЛОБАЛАМ функции, а не по имени модуля: conftest подменяет
    # `sys.modules["tools"]`, и `reg` здесь может оказаться ДРУГИМ объектом
    # модуля — тогда setattr бьёт мимо, дискавери идёт настоящая, а тест
    # зеленеет на том, что `office-mcp` не установлен.
    from gc_core.tools.connect_mcp.agent_tools import aget_mcp_tools_data_for_config

    # Идём по ЦЕПОЧКЕ от той функции, которую тест реально зовёт: conftest
    # подменяет `sys.modules["tools"]`, и `reg` тут может быть другим объектом
    # модуля — тогда и класс кэша окажется другим, и подмена уйдёт в пустоту.
    _prep = aget_mcp_tools_data_for_config.__globals__["_prepare_mcp_tools_data"]
    cache_cls = _prep.__globals__["ToolsCache"]
    monkeypatch.setitem(
        cache_cls.aget_tools.__globals__, "_abuild_tools_registry", fake_build
    )
    tools_data, status = await aget_mcp_tools_data_for_config(
        {"mcp-servers": {"office": {"transport": "stdio", "command": "office-mcp"}}}
    )
    assert status["office"]["status"] == "failed"
    # Текст — из подмены, а не из настоящего провала запуска процесса.
    assert status["office"]["reason"] == "сервер не отозвался"


async def test_build_tools_data_returns_mcp_status(monkeypatch):
    async def fake_mcp(config_dict, on_progress=None, **_kwargs):
        return ({}, {"x": {"status": "connected", "reason": None, "tool_count": 0, "elapsed_ms": 1}})
    # Подменяем НАСТОЯЩИЙ шов дискавери. Реэкспорт `get_mcp_tools_data` в
    # `gc_core.tools` был тонкой синхронной обёрткой и снят вместе с ней;
    # прод и раньше звал функцию из `agent_tools` напрямую.
    from gc_core.tools.connect_mcp import agent_tools as _at

    monkeypatch.setattr(_at, "aget_mcp_tools_data_for_config", fake_mcp)
    result = await tools_pkg.abuild_tools_data({"mcp-servers": {"x": {"transport": "stdio", "command": "x"}}})
    assert len(result) == 4
    assert result[3]["x"]["status"] == "connected"
