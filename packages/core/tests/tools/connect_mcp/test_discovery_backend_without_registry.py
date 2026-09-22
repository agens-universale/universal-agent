"""Дискавери без готового реестра идёт туда же, куда уйдут вызовы тулов.

`acall_mcp_tool` можно позвать без реестра — тогда она строит его сама, своим
`ToolsCache`. Бэкенд у неё на руках: это аргумент, который двумя строками ниже
уходит в `_prepare_mcp_call`. А кэшу он не передавался.

Кэш решает, где поднимать сервер, по наличию бэкенда: без него
`execute_on: os_backend` считается «нашим», и для stdio это ЗАПУСК КОМАНДЫ
СЕРВЕРА НА ХОСТЕ — мимо песочницы, ради которой `execute_on` и написан.
Вызовы тулов при этом уходят правильно; расходится именно дискавери, и
сказано об этом только в debug-лог.
"""

import sys

from gc_core.backends.protocol import ExecResult
from gc_core.tools.connect_mcp.api import acall_mcp_tool


class _Sandbox:
    """Песочница, которая ничего не исполняет, но всё записывает."""

    is_sandbox = True

    def __init__(self) -> None:
        self.calls: list = []

    async def aexec(self, command, **kw):
        self.calls.append(command)
        return ExecResult(return_code=1)

    async def aexec_argv(self, argv, **kw):
        self.calls.append(argv)
        return ExecResult(return_code=1)

    async def arun_python(self, *a, **kw):
        return ExecResult(return_code=1)


async def test_discovery_without_a_registry_does_not_start_the_server_on_the_host(tmp_path):
    """Команда сервера из конфига не должна исполниться в нашем процессе."""
    marker = tmp_path / "started-here"
    servers_cfg = {
        "fs": {
            "transport": "stdio",
            "execute_on": "os_backend",
            "command": sys.executable,
            "args": ["-c", f"open({str(marker)!r}, 'w').write('ran')"],
        }
    }
    backend = _Sandbox()

    try:
        await acall_mcp_tool(
            full_tool_name="fs__list_dir",
            arguments={},
            servers_cfg=servers_cfg,
            tools_registry=None,  # реестра нет — дискавери пойдёт прямо здесь
            timeout=5,
            os_backend=backend,
        )
    except Exception:
        # Песочница ничего не отвечает, тула в реестре не будет — исход вызова
        # здесь не при чём: проверяем МАРШРУТ дискавери.
        pass

    assert not marker.exists(), (
        "дискавери запустила команду сервера на хосте: кэш не знал про "
        "песочницу, и `execute_on: os_backend` обошёл её молча"
    )
    assert backend.calls, "дискавери не пошла в песочницу вовсе"
