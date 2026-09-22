"""`execute_on: os_backend` не уходит на хост: сборка тулов отдаёт бэкенд MCP.

develop передаёт `os_backend` в сборку MCP-тулов; при переводе на корутины
аргумент потерялся. Без него `runs_in_agent_runtime` считает любой сервер
«своим», и MCP с `execute_on: os_backend` молча исполнялся на хосте, мимо
песочницы.
"""


async def test_build_tools_data_hands_the_backend_to_mcp_discovery(monkeypatch):
    from gc_core import tools as tools_pkg
    from gc_core.tools.connect_mcp import agent_tools as at

    seen = {}

    async def _fake(config_dict, on_progress=None, *, tools_cache=None, os_backend=None):
        seen["os_backend"] = os_backend
        return {}, {}

    monkeypatch.setattr(at, "aget_mcp_tools_data_for_config", _fake)
    monkeypatch.setitem(
        tools_pkg.abuild_tools_data.__globals__, "aget_mcp_tools_data_for_config", _fake
    )
    sandbox = object()
    await tools_pkg.abuild_tools_data(
        {"mcp-servers": {"x": {"transport": "stdio", "command": "x",
                               "execute_on": "os_backend"}}},
        os_backend=sandbox,
    )
    assert "os_backend" in seen, "сборка MCP-тулов не вызывалась — проверяли не тот путь"
    assert seen["os_backend"] is sandbox
