"""exec_command / exec_python end-to-end: bounded capture, preview, отсутствие stream refs."""

import json
import sys
from types import SimpleNamespace

from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_core.tools.exec.run_python_code import RunPythonCode
from gc_core.execution.output_store import (
    ToolCallOutputsStore,
)
from gc_testkit.helpers.tool_ctx import with_local_ctx

CAP = 100
EDGE = 10


def _state():
    return SimpleNamespace(tool_call_outputs_store=ToolCallOutputsStore())


def _cfg(cap: int = CAP, edge: int = EDGE) -> dict:
    return {
        "agent.exec_output_capture_max_chars": cap,
        "agent.exec_output_preview_edge_chars": edge,
    }


def _tool(cls, *, state=None, config=None):
    return with_local_ctx(
        cls, state=state, call_id="call-1", config=config or _cfg()
    )


def _print_command(count: int) -> str:
    return (
        f'"{sys.executable}" -c "import sys; sys.stdout.write(\'A\'*{count})"'
    )


def test_short_output_is_whole_without_preview_or_refs():
    state = _state()
    r = json.loads(
        _tool(ExecuteCommand, state=state)(command=_print_command(5))
    )

    assert r["ok"] is True
    assert "truncated" not in r
    assert r["stdout"] == {"content": "AAAAA", "total_chars": 5}
    assert "preview" not in r
    assert "stdout_output_ref" not in r and "stderr_output_ref" not in r
    assert "hint" not in r
    assert len(state.tool_call_outputs_store) == 0


def test_stream_over_two_edges_gets_preview_while_content_stays_whole():
    r = json.loads(_tool(ExecuteCommand)(command=_print_command(50)))

    assert r["preview"]["stdout"] == {
        "head": "A" * EDGE,
        "tail": "A" * EDGE,
        "total_chars": 50,
        "omitted_chars": 30,
    }
    assert "truncated" not in r["stdout"]
    assert r["stdout"]["content"] == "A" * 50
    assert "truncated" not in r


def test_long_stream_is_head_tail_with_exact_counters():
    r = json.loads(_tool(ExecuteCommand)(command=_print_command(500)))

    out = r["stdout"]
    assert out["truncated"] is True
    assert "content" not in out
    assert len(out["head"]) == CAP // 2
    assert len(out["tail"]) == CAP - CAP // 2
    assert out["total_chars"] == 500
    assert out["omitted_chars"] == 400
    assert (
        len(out["head"]) + len(out["tail"]) + out["omitted_chars"]
        == out["total_chars"]
    )
    assert r["truncated"] is True
    assert r["preview"]["stdout"]["omitted_chars"] == 480


def test_real_tail_is_the_real_end_of_the_stream():
    code = (
        "import sys; sys.stdout.write('A'*1000); sys.stdout.write('THE-END')"
    )
    r = json.loads(
        _tool(ExecuteCommand)(command=f'"{sys.executable}" -c "{code}"')
    )

    assert r["stdout"]["tail"].endswith("THE-END")
    assert r["preview"]["stdout"]["tail"].endswith("THE-END")
    assert r["stdout"]["total_chars"] == 1007


def test_streams_are_independent():
    code = "import sys; sys.stdout.write('A'*5); sys.stderr.write('B'*500)"
    r = json.loads(
        _tool(ExecuteCommand)(command=f'"{sys.executable}" -c "{code}"')
    )

    assert r["stdout"] == {"content": "AAAAA", "total_chars": 5}
    assert "stdout" not in r["preview"]
    assert r["preview"]["stderr"]["total_chars"] == 500
    assert r["stderr"]["truncated"] is True


def test_no_hidden_four_thousand_limit():
    """Без max_output_chars=4000: 3500 символов при cap 10000 приходят целиком."""
    r = json.loads(
        _tool(ExecuteCommand, config=_cfg(cap=10000, edge=100))(
            command=_print_command(3500)
        )
    )

    assert "truncated" not in r["stdout"]
    assert r["stdout"]["total_chars"] == 3500
    assert len(r["stdout"]["content"]) == 3500


def test_max_output_chars_argument_is_ignored():
    """Скрытого второго лимита нет: аргумент тулы больше ничего не режет."""
    r = json.loads(
        _tool(ExecuteCommand)(command=_print_command(80), max_output_chars=5)
    )

    assert "truncated" not in r["stdout"]
    assert r["stdout"]["total_chars"] == 80


def test_exec_python_uses_the_same_contract():
    state = _state()
    r = json.loads(
        _tool(RunPythonCode, state=state)(
            code="import sys; sys.stdout.write('A'*500)"
        )
    )

    assert r["return_code"] == 0
    assert r["truncated"] is True
    assert r["stdout"]["total_chars"] == 500
    assert len(r["stdout"]["head"]) == CAP // 2
    assert r["preview"]["stdout"]["head"] == "A" * EDGE
    assert "stdout_output_ref" not in r
    assert len(state.tool_call_outputs_store) == 0


def test_exec_python_max_output_chars_argument_is_ignored():
    r = json.loads(
        _tool(RunPythonCode)(
            code="import sys; sys.stdout.write('A'*80)", max_output_chars=5
        )
    )

    assert "truncated" not in r["stdout"]
    assert r["stdout"]["total_chars"] == 80


def test_timeout_returns_observed_edges():
    code = "import sys, time; sys.stdout.write('A'*500); sys.stdout.flush(); time.sleep(30)"
    r = json.loads(
        _tool(ExecuteCommand)(
            command=f'"{sys.executable}" -c "{code}"', timeout=1
        )
    )

    assert r["timed_out"] is True
    assert r["exit_code"] is None
    assert r["stdout"]["total_chars"] == 500
    assert r["stdout"]["truncated"] is True
    assert r["preview"]["stdout"]["head"] == "A" * EDGE


async def test_backend_result_without_stream_structure_still_builds_envelope():
    """Backend вправе вернуть свой результат-подобный объект без CapturedStream.

    Форма вызова — `acall`: синхронного входа у exec-тулов нет, его сторожит
    `test_tools_are_async_only`. Утиный бэкенд объявляет только синхронный
    `exec` — его завернёт `SyncExecAdapter`, и это тоже часть проверки.
    """

    class _DuckBackend:
        is_sandbox = True
        exec_output_capture_max_chars = CAP

        def exec(
            self,
            command,
            *,
            cwd=None,
            timeout=None,
            env=None,
            capture_max_chars=None,
        ):
            return SimpleNamespace(
                stdout="hi", stderr="", return_code=0, timed_out=False
            )

        async def is_dir(self, path):
            return True

        def default_cwd(self):
            return None

    from gc_testkit.harbor_fakes import bind_os_backend
    from gc_testkit.helpers.tool_ctx import tool_settings

    tool = ExecuteCommand.__new__(ExecuteCommand)
    bind_os_backend(tool, _DuckBackend(), settings=tool_settings(_cfg()))

    r = json.loads(await tool.acall(command="x"))
    assert r["ok"] is True
    assert r["stdout"] == {"content": "hi", "total_chars": 2}
