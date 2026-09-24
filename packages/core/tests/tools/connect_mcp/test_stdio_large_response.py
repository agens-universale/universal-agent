"""Одноразовый stdio-обмен не должен спотыкаться о длину строки JSON-RPC.

``asyncio.create_subprocess_exec`` заводит ``StreamReader`` с потолком 64 КиБ,
и ``readline()`` на строке длиннее бросает ``ValueError: Separator is not
found, and chunk exceed the limit``, теряя буфер. Транспорт переезжал с
``subprocess.Popen(text=True)``, у которого потолка не было, — так регрессия и
проехала мимо всех наборов.

Порог перешагивает не экзотика: ``tools/list`` сервера на пару сотен
инструментов и любой крупный результат ``tools/call``. Снаружи это выглядит как
«сервер недоступен» на дискавери или ошибка тула.

Сервер здесь настоящий (отдельный процесс на stdin/stdout), а не подмена:
дефект живёт ровно в чтении трубы, и мок его бы не показал.
"""

import json
import sys

import pytest
from gc_core.tools.connect_mcp import transport

# Одной строкой, длиннее 64 КиБ: столько инструментов и отдаёт болтливый сервер.
_SERVER = r"""
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    method, rid = req.get("method"), req.get("id")
    if rid is None:
        continue
    if method == "initialize":
        body = {"protocolVersion": "2024-11-05", "capabilities": {},
                "serverInfo": {"name": "big", "version": "1"}}
    elif method == "tools/list":
        body = {"tools": [
            {"name": f"tool_{i:04d}",
             "description": "x" * 120,
             "inputSchema": {"type": "object", "properties": {}}}
            for i in range(600)
        ]}
    else:
        body = {"content": [{"type": "text", "text": "ok"}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": body}) + "\n")
    sys.stdout.flush()
"""


@pytest.fixture
def big_server_cfg(tmp_path):
    script = tmp_path / "big_stdio_server.py"
    script.write_text(_SERVER, encoding="utf-8")
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(script)],
    }


def test_the_response_really_is_over_the_default_limit(big_server_cfg):
    """Зонд обязан быть больше 64 КиБ, иначе он ничего не проверяет.

    Проверка не косметическая: подтяни кто-нибудь число инструментов вниз —
    и тест станет зелёным при вернувшемся дефекте, ничего не сказав.
    """
    tools = [
        {
            "name": f"tool_{i:04d}",
            "description": "x" * 120,
            "inputSchema": {"type": "object", "properties": {}},
        }
        for i in range(600)
    ]
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": tools}})

    assert len(line.encode()) > 64 * 1024, len(line)


_LIST_CALL = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


async def test_tools_list_longer_than_64k_is_read(big_server_cfg):
    """Ответ в одну длинную строку доходит целиком."""
    arequest = transport._arequest_stdio(big_server_cfg)
    _, body = await arequest("", {}, dict(_LIST_CALL), 30.0)

    tools = (body.get("result") or {}).get("tools") or []
    assert len(tools) == 600, (
        f"прочитано {len(tools)} инструментов: {str(body)[:200]}"
    )


def test_no_sync_exchange_remains():
    """Синхронного обмена по stdio больше нет.

    Здесь стоял его двойник: «синхронный вход идёт через тот же обмен и тоже
    обязан прочитать длинную строку». Синхронный слой транспорта снят вместе с
    маршрутом, который его звал, — свойство проверяется на корутинной форме
    тестом выше. У долгоживущего stdio свой путь, он к этому обмену не
    относится.
    """
    assert not hasattr(transport, "_request_stdio")
    assert not hasattr(transport, "_stdio_exchange")
    assert hasattr(transport, "_arequest_stdio")
