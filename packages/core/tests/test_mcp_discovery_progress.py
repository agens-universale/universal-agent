import time
from gc_core.tools.connect_mcp import registry as reg
from gc_testkit import as_coro


def test_on_progress_called_per_server(monkeypatch):
    def fake_discover_one(name, cfg, *, timeout, os_backend=None):
        return ([], {"status": "connected", "reason": None, "tool_count": 0, "elapsed_ms": 1})
    monkeypatch.setattr(reg, "_discover_one", fake_discover_one)
    seen = {}
    servers = {"a": {"transport": "stdio", "command": "a"},
               "b": {"transport": "stdio", "command": "b"}}
    reg._build_tools_registry(servers, discovery_timeout_sec=5,
                              on_progress=lambda s, st: seen.__setitem__(s, st["status"]))
    assert seen == {"a": "connected", "b": "connected"}


def test_on_progress_called_for_timeout(monkeypatch):
    def fake_discover_one(name, cfg, *, timeout, os_backend=None):
        time.sleep(timeout + 2)
        return ([], {"status": "connected", "reason": None, "tool_count": 0, "elapsed_ms": 0})
    monkeypatch.setattr(reg, "_discover_one", fake_discover_one)
    seen = {}
    reg._build_tools_registry({"hang": {"transport": "stdio", "command": "x"}},
                              discovery_timeout_sec=1,
                              on_progress=lambda s, st: seen.__setitem__(s, st))
    assert seen["hang"]["status"] == "failed"
    assert "таймаут" in seen["hang"]["reason"]


def test_on_progress_none_is_noop(monkeypatch):
    monkeypatch.setattr(reg, "_discover_one",
                        lambda n, c, *, timeout, os_backend=None: ([], {"status": "connected", "reason": None,
                                                       "tool_count": 0, "elapsed_ms": 1}))
    reg._build_tools_registry({"a": {"transport": "stdio", "command": "a"}}, discovery_timeout_sec=2)


async def test_on_progress_threads_through_build_tools_data(monkeypatch):
    from gc_core import tools as tools_pkg
    from gc_core.tools.connect_mcp import agent_tools

    captured = {}

    async def fake_get_mcp(config_dict, on_progress=None, **_kwargs):
        if on_progress:
            on_progress("srv", {"status": "connected"})
        return ({}, {})
    # Подменять надо КОРУТИННЫЙ шов: синхронного дискавери на этом пути
    # больше нет, и подмена синхронного имени просто ничего не делала бы —
    # тест ушёл бы в настоящую дискавери и покраснел «нет такого файла».
    monkeypatch.setattr(agent_tools, "aget_mcp_tools_data_for_config", fake_get_mcp)
    await tools_pkg.abuild_tools_data(
        {"mcp-servers": {"srv": {"transport": "stdio", "command": "x"}}},
        on_progress=lambda s, st: captured.__setitem__(s, st["status"]),
    )
    assert captured == {"srv": "connected"}


async def test_create_run_context_accepts_on_progress(monkeypatch):
    from gc_core import run_context as rc
    captured = []
    monkeypatch.setattr(rc, "abuild_tools_data", as_coro(
        lambda cfg, on_progress=None, **_kwargs: (captured.append(on_progress) or ({}, [], [], {}))))
    await rc.acreate_run_context({"model": "M"}, on_progress=lambda s, st: None)
    assert captured and callable(captured[0])
