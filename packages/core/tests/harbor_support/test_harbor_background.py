"""Фоновый запуск в Harbor без Docker: сборка строки, разбор pid, отказы.

Живой контейнер (`os_backends/test_harbor_live_container.py`) проверяет само
выживание, но без Docker пропускается целиком — и снятие `supports_background`
у Harbor, то есть возврат «фон в Harbor не работает», проходило молча. Здесь
окружение поддельное: оно запоминает строку, которую ему отдали, и отвечает
заготовленным выводом.
"""
import asyncio
import json
import shlex
import threading

import pytest
from gc_core.backends.harbor import HarborBackend
from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_testkit.harbor_fakes import bind_os_backend


class _Res:
    def __init__(self, stdout="", stderr="", return_code=0):
        self.stdout, self.stderr, self.return_code = stdout, stderr, return_code


class _Env:
    """Окружение Harbor, которое запоминает команды и отвечает заготовкой."""

    def __init__(self, stdout="4242\n", stderr="", exc=None):
        self.calls: list = []
        self._res = _Res(stdout, stderr)
        self._exc = exc

    async def exec(self, command, **kw):
        self.calls.append((command, kw))
        if self._exc is not None:
            raise self._exc
        return self._res


@pytest.fixture
def loop_in_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


def _tool(backend):
    tool = ExecuteCommand.__new__(ExecuteCommand)
    tool._config_dict = None
    return bind_os_backend(tool, backend, config_dict={})


def test_the_tool_starts_a_background_command_in_harbor(loop_in_thread):
    """Через тул, как видит модель: `ok`, pid из контейнера, без отказа «не умею»."""
    env = _Env(stdout="4242\n")

    res = json.loads(_tool(HarborBackend(env, loop_in_thread))(
        command="npm run dev", run_in_background=True,
    ))

    assert res["ok"] is True, res
    assert res["background"] == {"pid": 4242, "output": "not captured"}, res
    assert len(env.calls) == 1, "фоновый запуск — один exec в контейнере"


async def test_the_command_is_detached_inside_the_container(loop_in_thread):
    """Своя сессия, все потоки в /dev/null, pid — из `$!`, потолок на сам запуск."""
    env = _Env()

    await HarborBackend(env, loop_in_thread).aspawn_background("npm run dev")

    command, kw = env.calls[0]
    user = shlex.quote("npm run dev")
    assert f"setsid nohup bash -c {user} </dev/null >/dev/null 2>&1 &" in command, command
    assert f"else nohup bash -c {user} </dev/null >/dev/null 2>&1 &" in command, (
        f"нет запасного пути без setsid: {command}"
    )
    assert command.rstrip().endswith("echo $!"), command
    assert kw.get("timeout_sec") == 30, kw


async def test_the_launch_goes_through_the_model_channel(loop_in_thread):
    """bashrc, `env` и `cwd` — те же, что у переднего плана: канал модели."""
    env = _Env()
    backend = HarborBackend(env, loop_in_thread, exec_init="bashrc")

    await backend.aspawn_background("npm run dev", cwd="/app", env={"PORT": "3000"})

    command, _ = env.calls[0]
    assert command.startswith("cd /app && env PORT=3000 bash -c "), command
    assert "/etc/profile" in command, f"инициализация окружения потеряна: {command}"


async def test_the_pid_is_the_last_line_after_profile_noise(loop_in_thread):
    """Профиль оболочки печатает в stdout раньше нас: pid — последняя строка."""
    env = _Env(stdout="conda activated\nwelcome\n31337\n")

    started = await HarborBackend(env, loop_in_thread).aspawn_background("serve")

    assert started["pid"] == 31337, started


async def test_no_pid_is_an_error_not_a_bogus_process(loop_in_thread):
    """Оболочка pid не сообщила — отказ с её stderr, а не выдуманный pid."""
    env = _Env(stdout="welcome\n", stderr="bash: setsid: broken\n")

    with pytest.raises(RuntimeError, match="did not report its pid") as err:
        await HarborBackend(env, loop_in_thread).aspawn_background("serve")

    assert "setsid: broken" in str(err.value), err.value


async def test_a_timed_out_launch_says_so(loop_in_thread):
    """Таймаут запуска — не «pid не сообщён»: команда могла и стартовать.

    Прежде оба случая давали одно сообщение, и модель не могла понять, что
    делать: перезапускать (рискуя вторым экземпляром сервиса) или проверять.
    """
    env = _Env(exc=RuntimeError("Command timed out after 30 seconds"))

    with pytest.raises(RuntimeError) as err:
        await HarborBackend(env, loop_in_thread).aspawn_background("serve")

    message = str(err.value)
    assert "timed out" in message, message
    assert "did not report its pid" not in message, message
