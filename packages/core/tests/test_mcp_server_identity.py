"""Идентичность определения MCP-сервера: умолчания и отпечатки.

Отпечатки — ключи общих записей серверов, каталога тулов и сессий:
презентационные поля их не меняют, всё, что меняет соединение, — меняет.
"""

import pytest
from gc_core.config_modules.mcp import McpServerConfig
from gc_core.tools.connect_mcp.server_identity import (
    SESSION_KEY_FIELD,
    catalog_fingerprint,
    connection_fingerprint,
    is_process_transport,
    session_key,
    with_service_defaults,
)

STDIO = {"transport": "stdio", "command": "x"}


@pytest.mark.parametrize(
    ("transport", "expected"),
    [
        ("stdio", True),
        ("stream_stdio", True),
        ("http", False),
        ("sse", False),
        ("stream_http", False),
        ("stream_sse", False),
    ],
)
def test_default_is_persistent_for_process_transports_only(
    transport, expected
):
    cfg = {"transport": transport}
    assert is_process_transport(cfg) is expected
    assert with_service_defaults(cfg)["persistent_session"] is expected


def test_explicit_false_stays_false_and_input_is_not_mutated():
    cfg = {**STDIO, "persistent_session": False}
    assert with_service_defaults(cfg)["persistent_session"] is False
    unset = dict(STDIO)
    with_service_defaults(unset)
    assert "persistent_session" not in unset


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("whitelist", {"enabled": True, "tools": ["a"]}),
        ("description", "другое описание"),
        ("server-name", "other"),
        ("server_name", "other"),
        ("loop_control_tools", ["a"]),
        (SESSION_KEY_FIELD, "k@abc"),
    ],
)
def test_presentation_fields_do_not_change_fingerprint(field, value):
    base = {**STDIO, "url": "u", "args": ["a"]}
    assert connection_fingerprint({**base, field: value}) == (
        connection_fingerprint(base)
    )
    assert catalog_fingerprint({**base, field: value}) == (
        catalog_fingerprint(base)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("args", ["b"]),
        ("url", "other"),
        ("cwd", "/tmp"),
        ("env", {"A": "1"}),
        ("execute_on", "os_backend"),
        ("trace_header_enabled", True),
    ],
)
def test_connection_fields_change_fingerprint(field, value):
    base = {**STDIO, "url": "u", "args": ["a"], "execute_on": "agent"}
    changed = {**base, field: value}
    assert connection_fingerprint(changed) != connection_fingerprint(base)


def test_unset_and_explicit_default_have_one_fingerprint():
    explicit = {**STDIO, "persistent_session": True}
    assert connection_fingerprint(STDIO) == connection_fingerprint(explicit)
    assert connection_fingerprint(STDIO) != connection_fingerprint(
        {**STDIO, "persistent_session": False}
    )


def test_fingerprint_is_32_hex_and_ignores_key_order():
    a = {"transport": "stdio", "command": "x", "args": ["1"]}
    b = {"args": ["1"], "command": "x", "transport": "stdio"}
    fp = connection_fingerprint(a)
    assert fp == connection_fingerprint(b)
    assert len(fp) == 32
    assert set(fp) <= set("0123456789abcdef")


def test_secret_change_keeps_catalog_fingerprint():
    base = {
        "transport": "http",
        "url": "u",
        "token": "t1",
        "headers": {"X-Key": "v1"},
        "env": {"A": "1"},
    }
    for field, value in (
        ("token", "t2"),
        ("headers", {"X-Key": "v2"}),
        ("env", {"A": "2"}),
    ):
        changed = {**base, field: value}
        assert connection_fingerprint(changed) != connection_fingerprint(base)
        assert catalog_fingerprint(changed) == catalog_fingerprint(base)
    renamed = {**base, "env": {"A": "1", "B": "2"}}
    assert connection_fingerprint(renamed) != connection_fingerprint(base)
    assert catalog_fingerprint(renamed) != catalog_fingerprint(base)


def test_token_presence_changes_catalog_fingerprint():
    base = {"transport": "http", "url": "u"}
    with_token = {**base, "token": "t"}
    assert catalog_fingerprint(with_token) != catalog_fingerprint(base)
    assert catalog_fingerprint(with_token) == catalog_fingerprint(
        {**base, "token": "other"}
    )


def test_session_key_joins_server_key_and_fingerprint():
    assert session_key("srv", "abc") == "srv@abc"


def test_schema_accepts_unset_persistent_session():
    cfg = McpServerConfig(transport="stdio", command="x")
    assert cfg.persistent_session is None
