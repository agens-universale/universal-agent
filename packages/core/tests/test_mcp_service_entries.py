"""Сервис MCP: общие записи серверов, каталог и классификация отказов.

Запись — общие для потребителей факты о сервере (сырой список тулов,
время проверки, последний отказ) по паре «ключ, отпечаток». Сервис в сеть
не ходит: записи обновляют исходы подключений потребителей.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from gc_core.config_modules.field_meta import plain_dump
from gc_core.config_modules.root import AppConfig
from gc_core.tools.connect_mcp.call_errors import (
    McpCallTimedOut,
    McpServerGone,
)
from gc_core.tools.connect_mcp.catalog import read_catalog
from gc_core.tools.connect_mcp.client import McpProtocolError
from gc_core.tools.connect_mcp.durable_scope import McpSessionLostError
from gc_core.tools.connect_mcp.effective import effective_mcp_servers
from gc_core.tools.connect_mcp.service import McpService, classify_failure
from gc_core.tools.connect_mcp.service_records import Failure
from gc_core.tools.connect_mcp.stream_client import McpToolCallError
from gc_testkit.mcp import FakeClock, seed_catalog, stdio_cfg
from gc_testkit.mcp.fixtures import mcp_home
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED, ErrorData

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
_TOKEN = "tok-SECRET-1"
_HEADER = "hdr-SECRET-2"
_ENV = "env-SECRET-3"
_URL = "http://127.0.0.1:1/mcp"


def _settings(servers: dict) -> AppConfig:
    return AppConfig.model_validate({"mcp": {"servers": servers}})


def _definition(settings: AppConfig, key: str) -> dict:
    """Определение так, как его строит хост: по алиасам, без trace."""
    return plain_dump(effective_mcp_servers(settings)[key], by_alias=True)


def _stdio_definition(tmp_path, key: str = "a", **extra) -> dict:
    cfg = {**stdio_cfg(tmp_path / f"{key}.json"), **extra}
    return _definition(_settings({key: cfg}), key)


async def test_new_entry_takes_tools_from_catalog(mcp_home, tmp_path):
    settings = _settings(
        {
            "a": stdio_cfg(tmp_path / "a.json"),
            "b": stdio_cfg(tmp_path / "b.json"),
        }
    )
    seed_catalog(settings, "a", ["echo", "nap"])
    service = McpService(connect_on_start=True)

    a = service.entry("a", _definition(settings, "a"), discovery_timeout=5)
    b = service.entry("b", _definition(settings, "b"), discovery_timeout=5)

    assert a.tools == ({"name": "echo"}, {"name": "nap"})
    assert a.from_catalog is True
    assert a.checked_at is None
    assert b.tools == ()
    assert b.from_catalog is False


async def test_definition_keeps_aliases(mcp_home):
    settings = _settings(
        {
            "a": {
                "transport": "stream_http",
                "url": _URL,
                "verify-ssl": False,
                "client-cert": "/certs/client.pem",
            }
        }
    )
    service = McpService(connect_on_start=True)

    entry = service.entry("a", _definition(settings, "a"), discovery_timeout=5)

    assert entry.cfg["verify-ssl"] is False
    assert "client-cert" in entry.cfg
    assert "verify_ssl" not in entry.cfg
    assert "client_cert" not in entry.cfg
    # Умолчание сервиса раскрыто: сетевой сервер без флага — разовый.
    assert entry.cfg["persistent_session"] is False


async def test_same_definition_shares_entry(mcp_home, tmp_path):
    service = McpService(connect_on_start=True)
    first = service.entry(
        "a", _stdio_definition(tmp_path), discovery_timeout=5
    )

    same = service.entry("a", _stdio_definition(tmp_path), discovery_timeout=7)
    listed = service.entry(
        "a",
        _stdio_definition(
            tmp_path, whitelist={"enabled": True, "tools": ["echo"]}
        ),
        discovery_timeout=7,
    )
    other_args = service.entry(
        "a", _stdio_definition(tmp_path, args=["-V"]), discovery_timeout=7
    )

    assert same is first
    assert listed is first
    assert other_args is not first
    assert other_args.fingerprint != first.fingerprint
    # Срок подключения — из настроек последнего обращения.
    assert first.discovery_timeout == 7
    assert first.cfg["persistent_session"] is True


async def test_success_writes_catalog_only_on_change(mcp_home, tmp_path):
    clock = FakeClock()
    service = McpService(connect_on_start=True, clock=clock.now)
    cfg = _stdio_definition(tmp_path)
    entry = service.entry("a", cfg, discovery_timeout=5)
    tools = [{"name": "echo"}]
    changed = [{"name": "echo"}, {"name": "nap"}]

    clock.advance(3)
    service.record_success(entry, tools)
    assert read_catalog("a", cfg) == tools
    assert (entry.tools, entry.checked_at) == (tuple(tools), 3)
    catalog_files = list((mcp_home / "cache").rglob("*.json"))
    assert len(catalog_files) == 1
    catalog_files[0].unlink()

    clock.advance(2)
    service.record_success(entry, list(tools))
    assert read_catalog("a", cfg) is None
    assert entry.checked_at == 5

    service.record_success(entry, changed)
    assert read_catalog("a", cfg) == changed
    assert entry.tools == tuple(changed)
    assert entry.from_catalog is False


async def test_first_success_with_empty_list_is_cataloged(mcp_home, tmp_path):
    """Пустой список — известный ответ сервера, а не «тулов не знаем»."""
    service = McpService(connect_on_start=True)
    cfg = _stdio_definition(tmp_path)
    entry = service.entry("a", cfg, discovery_timeout=5)

    service.record_success(entry, [])

    assert read_catalog("a", cfg) == []
    assert entry.checked_at is not None


async def test_success_after_catalog_with_same_list_keeps_file(
    mcp_home, tmp_path
):
    settings = _settings({"a": stdio_cfg(tmp_path / "a.json")})
    seed_catalog(settings, "a", ["echo"])
    service = McpService(connect_on_start=True)
    cfg = _definition(settings, "a")
    entry = service.entry("a", cfg, discovery_timeout=5)
    next((mcp_home / "cache").rglob("*.json")).unlink()

    service.record_success(entry, [{"name": "echo"}])

    assert read_catalog("a", cfg) is None
    assert entry.from_catalog is False


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", _URL)
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(
        f"status {code}", request=request, response=response
    )


def _secret_definition(**secrets) -> dict:
    """Сетевое определение с секретами — так, как его строит хост."""
    server = {"transport": "stream_http", "url": _URL, "command": "npx"}
    return _definition(_settings({"a": {**server, **secrets}}), "a")


def _reason(exc: BaseException, **secrets) -> str:
    return classify_failure(exc, _secret_definition(**secrets))[1]


_FAILURES = [
    # В 3.11+ asyncio.TimeoutError — встроенный TimeoutError.
    (asyncio.TimeoutError(), "timeout"),
    (McpCallTimedOut(5), "timeout"),
    (httpx.ConnectTimeout("connect"), "timeout"),
    (McpError(ErrorData(code=408, message="Timed out")), "timeout"),
    (FileNotFoundError("npx"), "command_not_found"),
    (_status_error(401), "auth"),
    (_status_error(403), "auth"),
    (_status_error(500), "other"),
    (McpProtocolError("сервер не вернул список тулов"), "protocol"),
    (ConnectionRefusedError(), "connect_error"),
    (ConnectionError("initialize: сервер завершился"), "connect_error"),
    (httpx.ConnectError("refused"), "connect_error"),
    (McpServerGone(), "connect_error"),
    (McpSessionLostError("сервер завершился, не ответив"), "connect_error"),
    (
        McpError(ErrorData(code=CONNECTION_CLOSED, message="closed")),
        "connect_error",
    ),
    (ExceptionGroup("g", [OSError()]), "connect_error"),
    (
        ExceptionGroup("g", [ExceptionGroup("h", [_status_error(401)])]),
        "auth",
    ),
    (McpError(ErrorData(code=-32601, message="no method")), "other"),
    (RuntimeError(f"bad {_TOKEN} {_HEADER} {_ENV}"), "other"),
]


@pytest.mark.parametrize(
    ("exc", "code"),
    _FAILURES,
    ids=[f"{type(e).__name__}-{c}" for e, c in _FAILURES],
)
async def test_failure_keeps_tools_and_classifies(mcp_home, exc, code):
    clock = FakeClock()
    service = McpService(connect_on_start=True, clock=clock.now)
    cfg = _secret_definition(
        token=_TOKEN, headers={"X-Key": _HEADER}, env={"API_KEY": _ENV}
    )
    entry = service.entry("a", cfg, discovery_timeout=5)
    service.record_success(entry, [{"name": "echo"}])
    clock.advance(4)

    failure = service.record_failure(entry, exc)

    assert failure.code == code
    assert failure.at == 4
    assert failure.reason
    assert entry.last_failure is failure
    assert entry.tools == ({"name": "echo"},)
    for secret in (_TOKEN, _HEADER, _ENV):
        assert secret not in failure.reason


def _fragments(secret: str, size: int = 4) -> list[str]:
    return [secret[i : i + size] for i in range(len(secret) - size + 1)]


def test_secret_on_truncation_boundary_is_masked_whole():
    """Маскировка идёт до обрезки длинного текста: от секрета на границе
    среза не остаётся ни одного фрагмента."""
    token = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"
    message = "." * 101 + " " + token + " конец"

    reason = _reason(RuntimeError(message), token=token)

    assert reason.startswith("." * 101)
    for fragment in _fragments(token):
        assert fragment not in reason


def test_parts_of_header_value_are_masked():
    """Ответ сервера несёт ключ без префикса значения заголовка."""
    key = "KEY-0123456789"

    reason = _reason(
        RuntimeError(f"invalid token {key}"),
        headers={"Authorization": f"Bearer {key}"},
    )

    assert key not in reason
    assert reason == "invalid token ***"


def test_short_secret_is_masked_as_a_separate_word():
    reason = _reason(
        RuntimeError("wrong pin abc1234, pinabc1234x stays"),
        env={"PIN": "abc1234"},
    )

    assert reason == "wrong pin ***, pinabc1234x stays"


def test_secret_in_url_is_masked_in_connect_reason():
    key = "url-SECRET-0123"

    reason = _reason(
        ConnectionRefusedError(),
        url=f"https://h/mcp?api_key={key}",
        headers={"X-Api-Key": key},
    )

    assert reason == "нет соединения с https://h/mcp?api_key=***"


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        (
            "https://h/mcp?apikey=URL-SECRET-0123&v=1",
            "https://h/mcp?apikey=***&v=***",
        ),
        ("https://h/mcp?key=ab%2Bcd%2Fefgh1234", "https://h/mcp?key=***"),
        ("https://h/mcp?key=abc+defgh1234", "https://h/mcp?key=***"),
        (
            "http://localhost:8001/mcp?server=localhost",
            "http://localhost:8001/mcp?server=***",
        ),
        ("http://h:8001/mcp?port=8001", "http://h:8001/mcp?port=***"),
        (
            "https://h/mcp/tools?path=/mcp/tools",
            "https://h/mcp/tools?path=***",
        ),
        ("https://user:pass-SECRET@h/mcp", "https://user:***@h/mcp"),
    ],
    ids=["plain", "percent", "plus", "host", "port", "path", "password"],
)
def test_url_secrets_are_masked_in_place(url, shown):
    """Секреты адреса маскируются по месту, как записаны в определении:
    хост, порт и путь целы, даже если значение параметра их повторяет."""
    reason = _reason(ConnectionRefusedError(), url=url)

    assert reason == f"нет соединения с {shown}"


def test_http_status_reason_has_no_address():
    """Причина ответа HTTP — код и фраза, без адреса и ссылки httpx."""
    request = httpx.Request("POST", "https://h/mcp?apikey=URL-SECRET-0123")
    response = httpx.Response(401, request=request)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()

    reason = _reason(caught.value)

    assert reason == "сервер ответил 401 Unauthorized"


@pytest.mark.parametrize(
    "exc",
    [
        McpError(
            ErrorData(code=CONNECTION_CLOSED, message="Connection closed")
        ),
        McpToolCallError(
            ErrorData(code=CONNECTION_CLOSED, message="Connection closed")
        ),
        McpServerGone(),
    ],
    ids=["sdk", "wrapped", "stdio"],
)
def test_connection_lost_mid_request_has_one_text(exc):
    assert _reason(exc) == "сервер завершился, не ответив"


@pytest.mark.parametrize(
    "exc", [httpx.ConnectTimeout(""), httpx.PoolTimeout("")]
)
def test_connect_timeout_reason_is_readable(exc):
    assert _reason(exc) == f"нет соединения с {_URL}: истёк срок подключения"


def test_secret_in_command_is_masked_in_not_found_reason():
    key = "cmd-SECRET-0123"

    reason = _reason(
        FileNotFoundError(), command=f"run --key {key}", env={"KEY": key}
    )

    assert reason == "команда 'run --key ***' не найдена"


@pytest.mark.parametrize(
    "exc",
    [McpCallTimedOut(10), RuntimeError("untrue 127.0.0.1:8001 за 10 с")],
    ids=["timeout", "address"],
)
def test_tiny_env_values_do_not_spoil_text(exc):
    plain = _reason(exc)

    spoiled = _reason(exc, env={"DEBUG": "1", "VERBOSE": "true"})

    assert spoiled == plain
    assert "***" not in spoiled


def test_failure_is_frozen():
    failure = Failure(at=1.0, code="other", reason="r")

    with pytest.raises(AttributeError):
        failure.code = "timeout"  # type: ignore[misc]


async def test_service_on_another_loop_is_rejected(mcp_home, tmp_path):
    service = McpService(connect_on_start=True)
    cfg = _stdio_definition(tmp_path)
    service.entry("a", cfg, discovery_timeout=5)

    async def touch() -> None:
        service.entry("a", cfg, discovery_timeout=5)

    def on_other_loop() -> BaseException | None:
        try:
            asyncio.run(touch())
        except BaseException as exc:
            return exc
        return None

    error = await asyncio.wait_for(
        asyncio.to_thread(on_other_loop), _CEILING_SEC
    )

    assert isinstance(error, RuntimeError)
    assert "другом цикле" in str(error)
    # Свой цикл по-прежнему обслуживается.
    service.entry("a", cfg, discovery_timeout=5)


def test_stop_event_of_unbound_service_can_be_set():
    """Сервис, ни разу не привязанный к циклу, можно пометить остановленным
    (так закроет его aclose), а ожидание остановки потом не ломается."""
    service = McpService(connect_on_start=False)

    service._stopped = True
    service._stopped_event.set()

    async def wait_stopped() -> None:
        await asyncio.wait_for(service._stopped_event.wait(), _CEILING_SEC)

    asyncio.run(wait_stopped())
    assert service.closed is True
