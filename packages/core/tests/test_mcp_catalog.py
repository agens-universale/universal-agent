"""Каталог тулов MCP на диске и сырой список тулов сервера.

Каталог хранит сырой ответ ``tools/list`` по определению сервера, чтобы
следующий запуск видел тулы сразу. Записи реестра из сырого списка строит
та же функция, что и дискавери; ответ без списка тулов в строгом режиме —
отказ, а не пустой список.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading

import pytest
from gc_core.config_modules.field_meta import plain_dump
from gc_core.config_modules.root import AppConfig
from gc_core.tools.connect_mcp import registry as reg
from gc_core.tools.connect_mcp.catalog import (
    CATALOG_KEEP,
    read_catalog,
    write_catalog,
)
from gc_core.tools.connect_mcp.client import McpProtocolError
from gc_core.tools.connect_mcp.effective import effective_mcp_servers
from gc_core.tools.connect_mcp.registry import (
    alist_raw_tools,
    build_registry_entries,
    mcp_tool_full_name,
)
from gc_testkit.mcp import events, seed_catalog, set_behavior, stdio_cfg
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
_TOKEN = "tok-SECRET-1"
_HEADER = "hdr-SECRET-2"
_ENV = "env-SECRET-3"
_TOOLS = [
    {"name": "echo", "description": "эхо", "inputSchema": {"type": "object"}}
]
_posix = pytest.mark.skipif(os.name != "posix", reason="процессы POSIX")


def _http_cfg(token: str = _TOKEN, **extra) -> dict:
    return {
        "transport": "stream_http",
        "url": "http://127.0.0.1:1/mcp",
        "token": token,
        "headers": {"X-Key": _HEADER},
        "env": {"API_KEY": _ENV},
        **extra,
    }


def _catalog_files(home) -> list:
    return sorted((home / "cache" / "mcp" / "catalog").rglob("*"))


def _json_files(home) -> list:
    return [p for p in _catalog_files(home) if p.suffix == ".json"]


def test_roundtrip_and_no_secrets_on_disk(mcp_home):
    write_catalog("srv/α", _http_cfg(), _TOOLS)

    assert read_catalog("srv/α", _http_cfg()) == _TOOLS
    files = _json_files(mcp_home)
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    for secret in (_TOKEN, _HEADER, _ENV):
        assert secret not in str(files[0].relative_to(mcp_home))
        assert secret not in text
    assert json.loads(text) == {"format": 1, "tools": _TOOLS}
    other = _http_cfg("tok-OTHER", headers={"X-Key": "другой"})
    assert read_catalog("srv/α", other) == _TOOLS


def test_corrupt_and_foreign_format_are_missing(mcp_home):
    cfg = _http_cfg()
    write_catalog("srv", cfg, _TOOLS)
    (path,) = _json_files(mcp_home)

    path.write_text("{не json", encoding="utf-8")
    assert read_catalog("srv", cfg) is None

    path.write_text(json.dumps({"format": 99, "tools": _TOOLS}))
    assert read_catalog("srv", cfg) is None

    path.write_text(json.dumps({"format": 1, "tools": {"echo": 1}}))
    assert read_catalog("srv", cfg) is None


def test_sixth_fingerprint_evicts_oldest(mcp_home):
    stamp = 1_700_000_000
    seen: set[str] = set()
    for i in range(CATALOG_KEEP + 1):
        cfg = {"transport": "stdio", "command": "x", "args": [str(i)]}
        write_catalog("srv", cfg, [{"name": f"t{i}"}])
        (new,) = [p for p in _json_files(mcp_home) if p.name not in seen]
        seen.add(new.name)
        os.utime(new, (stamp + i, stamp + i))

    assert len(_json_files(mcp_home)) == CATALOG_KEEP
    first = {"transport": "stdio", "command": "x", "args": ["0"]}
    assert read_catalog("srv", first) is None
    last = {"transport": "stdio", "command": "x", "args": ["5"]}
    assert read_catalog("srv", last) == [{"name": "t5"}]


@_posix
@pytest.mark.skipif(
    os.name == "posix" and os.geteuid() == 0, reason="root пишет всюду"
)
def test_unwritable_dir_does_not_raise(mcp_home):
    cfg = _http_cfg()
    write_catalog("srv", cfg, _TOOLS)
    (path,) = _json_files(mcp_home)
    path.unlink()
    key_dir = path.parent
    key_dir.chmod(0o500)
    try:
        write_catalog("srv", cfg, _TOOLS)
        assert read_catalog("srv", cfg) is None
    finally:
        key_dir.chmod(0o700)


def test_concurrent_writers_leave_valid_file(mcp_home):
    cfg = _http_cfg()

    def _writer(tag: str) -> None:
        for i in range(50):
            write_catalog("srv", cfg, [{"name": f"{tag}{i}"}])

    threads = [threading.Thread(target=_writer, args=(t,)) for t in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_CEILING_SEC)

    assert not any(thread.is_alive() for thread in threads)
    tools = read_catalog("srv", cfg)
    assert tools in ([{"name": "a49"}], [{"name": "b49"}])
    assert len(_catalog_files(mcp_home)) == 2  # каталог ключа и один файл


def test_seed_catalog_writes_what_the_check_reads(mcp_home, tmp_path):
    control = tmp_path / "s.json"
    settings = AppConfig.model_validate(
        {"mcp": {"servers": {"srv": {**stdio_cfg(control), "token": "t"}}}}
    )

    seed_catalog(settings, "srv", ["echo", "nap"])

    model = effective_mcp_servers(settings)["srv"]
    cfg = plain_dump(model, by_alias=True)
    assert read_catalog("srv", cfg) == [{"name": "echo"}, {"name": "nap"}]
    assert events(control) == []


def _whitelisted(control) -> dict:
    return {
        **stdio_cfg(control),
        "whitelist": {"enabled": True, "tools": ["echo"]},
    }


@_posix
async def test_entries_from_raw_equal_registry(
    mcp_home, mcp_reaper, mcp_owner, tmp_path
):
    cfg = _whitelisted(tmp_path / "s.json")

    raw = await asyncio.wait_for(
        alist_raw_tools("srv", cfg, timeout=20), _CEILING_SEC
    )
    expected, _status = await asyncio.wait_for(
        reg._abuild_tools_registry({"srv": cfg}, discovery_timeout_sec=20),
        _CEILING_SEC,
    )

    assert [t["name"] for t in raw] == ["echo", "nap"]
    entries = build_registry_entries("srv", cfg, raw)
    assert entries == expected
    assert list(entries) == [mcp_tool_full_name("srv", "echo")]


def test_quiet_entries_do_not_log_whitelist(caplog):
    cfg = {
        "transport": "stdio",
        "command": "x",
        "whitelist": {"enabled": True, "tools": ["echo", "gone"]},
    }
    raw = [{"name": "echo"}, {"name": "nap"}]

    with caplog.at_level(logging.INFO, logger=reg.__name__):
        quiet = build_registry_entries("srv", cfg, raw, quiet=True)
    assert caplog.records == []

    with caplog.at_level(logging.INFO, logger=reg.__name__):
        loud = build_registry_entries("srv", cfg, raw)
    assert quiet == loud
    assert len(caplog.records) == 2


_SESSIONS = pytest.mark.parametrize(
    "persistent", [True, False], ids=["persistent", "one_shot"]
)


@_posix
@_SESSIONS
async def test_strict_list_rejects_missing_tools(
    persistent, mcp_home, mcp_reaper, mcp_owner, tmp_path
):
    control = tmp_path / "s.json"
    cfg = {**stdio_cfg(control), "persistent_session": persistent}
    set_behavior(control, no_tools_key=True)

    with pytest.raises(McpProtocolError, match="не вернул список тулов"):
        await asyncio.wait_for(
            alist_raw_tools("srv", cfg, timeout=20), _CEILING_SEC
        )

    set_behavior(control, tools=[])
    raw = await asyncio.wait_for(
        alist_raw_tools("srv", cfg, timeout=20), _CEILING_SEC
    )
    assert raw == []


@_posix
@_SESSIONS
async def test_non_strict_path_unchanged(
    persistent, mcp_home, mcp_reaper, mcp_owner, tmp_path
):
    control = tmp_path / "s.json"
    cfg = {**stdio_cfg(control), "persistent_session": persistent}
    set_behavior(control, no_tools_key=True)

    tools, status = await asyncio.wait_for(
        reg._adiscover_one("srv", cfg, timeout=20), _CEILING_SEC
    )

    assert status["status"] == "connected"
    assert (tools, status["tool_count"]) == ([], 0)
