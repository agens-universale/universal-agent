import json
import sys

import pytest

from gc_core.backends.local import LocalFsBackend
from gc_core.backends.protocol import ExecResult, FsBackend
from gc_core.tools.skills.registry import SkillsRegistry
from gc_core.tools.skills.skills_tools import RunSkillScriptTool
from gc_testkit.helpers.tool_ctx import local_ctx
from gc_testkit.helpers.tool_ctx import CallableTool


def _write_skill_with_script(root, skill_id: str, script_body: str) -> None:
    skill_dir = root / skill_id
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nid: {skill_id}\nname: {skill_id}\ndescription: \"\"\n---\n# body\n",
        encoding="utf-8",
    )
    (scripts_dir / "main.py").write_text(script_body, encoding="utf-8")


@pytest.fixture
def reg_with_scripts(tmp_path):
    system = tmp_path / "system"
    system.mkdir()
    return SkillsRegistry(
        system_root=str(system),
        user_root=str(tmp_path / "user"),
        project_root=str(tmp_path / "project"),
    )


def test_skills_run_script_sets_script_ok_false_on_nonzero_exit(reg_with_scripts, tmp_path):
    _write_skill_with_script(
        tmp_path / "system",
        "exit-one",
        "import sys\nsys.exit(1)\n",
    )
    tool = CallableTool(RunSkillScriptTool(reg_with_scripts, ctx=local_ctx()))
    payload = json.loads(tool(skill_id="exit-one", script_name="main.py"))
    assert payload["ok"] is True
    assert payload["script_ok"] is False
    assert payload["result"]["exit_code"] == 1


def test_skills_run_script_sets_script_ok_true_on_zero_exit(reg_with_scripts, tmp_path):
    _write_skill_with_script(
        tmp_path / "system",
        "exit-zero",
        "import sys\nprint('done')\nsys.exit(0)\n",
    )
    tool = CallableTool(RunSkillScriptTool(reg_with_scripts, ctx=local_ctx()))
    payload = json.loads(tool(skill_id="exit-zero", script_name="main.py"))
    assert payload["ok"] is True
    assert payload["script_ok"] is True
    assert payload["result"]["exit_code"] == 0
    assert payload["result"]["truncated"] is False
    assert payload["result"]["stdout"]["content"].strip() == "done"
    assert "preview" not in payload["result"]


def test_skills_run_script_passes_args_as_stdin_json(reg_with_scripts, tmp_path):
    _write_skill_with_script(
        tmp_path / "system",
        "stdin-echo",
        (
            "import json, sys\n"
            "payload = json.load(sys.stdin)\n"
            "print(payload.get('skill_path', ''))\n"
        ),
    )
    tool = CallableTool(RunSkillScriptTool(reg_with_scripts, ctx=local_ctx()))
    payload = json.loads(
        tool(
            skill_id="stdin-echo",
            script_name="main.py",
            args='{"skill_path":"/tmp/my-skill"}',
        )
    )
    assert payload["ok"] is True
    assert payload["script_ok"] is True
    assert payload["result"]["stdout"]["content"].strip() == "/tmp/my-skill"


def test_skills_run_script_passes_argv(reg_with_scripts, tmp_path):
    _write_skill_with_script(
        tmp_path / "system",
        "argv-echo",
        "import sys\nprint('|'.join(sys.argv[1:]))\n",
    )
    tool = CallableTool(RunSkillScriptTool(reg_with_scripts, ctx=local_ctx()))
    payload = json.loads(
        tool(skill_id="argv-echo", script_name="main.py", argv='--format json "two words"')
    )
    assert payload["ok"] is True
    assert payload["script_ok"] is True
    assert payload["result"]["stdout"]["content"].strip() == "--format|json|two words"


async def test_skills_run_script_long_stdout_is_head_tail_with_preview(reg_with_scripts, tmp_path):
    """Форма вызова — `acall`: синхронного входа у тула нет (async-only ядро)."""
    _write_skill_with_script(
        tmp_path / "system",
        "long-out",
        "import sys; sys.stdout.write('A' * 500)\n",
    )
    cfg = {"exec_output_capture_max_chars": 100, "exec_output_preview_edge_chars": 10}
    tool = RunSkillScriptTool(reg_with_scripts, ctx=local_ctx(cfg), config_dict=cfg)
    payload = json.loads(await tool.acall(skill_id="long-out", script_name="main.py"))
    result = payload["result"]
    out = result["stdout"]

    assert payload["ok"] is True
    assert payload["script_ok"] is True
    assert result["truncated"] is True
    assert out["truncated"] is True
    assert "content" not in out
    assert len(out["head"]) == 50
    assert len(out["tail"]) == 50
    assert out["total_chars"] == 500
    assert out["omitted_chars"] == 400
    assert result["preview"]["stdout"]["head"] == "A" * 10
    assert result["preview"]["stdout"]["tail"] == "A" * 10
    raw = json.dumps(result, ensure_ascii=False)
    assert raw.index('"preview"') < raw.index('"stdout"')


def test_local_exec_argv_is_shell_less_and_preserves_hostile_tokens(tmp_path):
    """Tokens that break shell quoting (Windows paths, quotes) pass verbatim.

    This is the cross-platform guarantee: an argv list goes through
    CreateProcess/execvp without any shell, so there is nothing to misquote.
    """
    script = tmp_path / "echo_argv.py"
    script.write_text(
        "import sys\nprint('|'.join(sys.argv[1:]))\nprint(sys.stdin.read())\n",
        encoding="utf-8",
    )
    hostile = [r"C:\temp\it's report.pdf", '{"a": "b c"}', "--flag='x'"]
    res = LocalFsBackend().exec_argv(
        [sys.executable, str(script), *hostile], stdin="42"
    )
    assert res.return_code == 0
    lines = res.stdout.splitlines()
    assert lines[0] == "|".join(hostile)
    assert lines[1] == "42"


def test_tracking_fs_backend_exec_argv_is_shell_less_and_delegates(tmp_path):
    """TrackingFsBackend (TUI wrapper) must expose exec_argv and delegate
    to the inner LocalFsBackend without a POSIX shell string.

    Regression for skills_run_script under TUI: the Tracking decorator
    previously had no exec_argv method, so ``backend.exec_argv(...)``
    raised AttributeError. argv must pass through verbatim (shell-less).
    """
    from gc_tui.diff_tui.tracking import TrackingFsBackend

    script = tmp_path / "echo_argv.py"
    script.write_text(
        "import sys\nprint('|'.join(sys.argv[1:]))\nprint(sys.stdin.read())\n",
        encoding="utf-8",
    )
    hostile = [r"C:\temp\it's report.pdf", '{"a": "b c"}', "--flag='x'"]
    inner = LocalFsBackend()
    ctx = local_ctx()
    wrapped = TrackingFsBackend(inner, ctx)
    res = wrapped.exec_argv(
        [sys.executable, str(script), *hostile], stdin="42"
    )
    assert res.return_code == 0
    lines = res.stdout.splitlines()
    assert lines[0] == "|".join(hostile)
    assert lines[1] == "42"


def test_default_exec_argv_builds_posix_pipe_for_sandbox_backends():
    """Sandbox backends (harbor) keep the POSIX shell-string contract."""
    class _Recorder:
        is_sandbox = True

        def exec(self, command, *, cwd=None, timeout=None, env=None):
            self.command = command
            return ExecResult(stdout="", stderr="", return_code=0)

    rec = _Recorder()
    FsBackend.exec_argv(rec, ["python3", "/harbor/skills/s/scripts/x.py", "a b"], stdin='{"k": 1}')
    assert rec.command == (
        "printf '%s' '{\"k\": 1}' | python3 /harbor/skills/s/scripts/x.py 'a b'"
    )
