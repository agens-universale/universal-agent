import json

import pytest

from tools.skills.registry import SkillsRegistry
from tools.skills.skills_tools import RunSkillScriptTool


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
    tool = RunSkillScriptTool(reg_with_scripts)
    payload = json.loads(tool(skill_id="exit-one"))
    assert payload["ok"] is True
    assert payload["script_ok"] is False
    assert payload["result"]["exit_code"] == 1


def test_skills_run_script_sets_script_ok_true_on_zero_exit(reg_with_scripts, tmp_path):
    _write_skill_with_script(
        tmp_path / "system",
        "exit-zero",
        "import sys\nprint('done')\nsys.exit(0)\n",
    )
    tool = RunSkillScriptTool(reg_with_scripts)
    payload = json.loads(tool(skill_id="exit-zero"))
    assert payload["ok"] is True
    assert payload["script_ok"] is True
    assert payload["result"]["exit_code"] == 0
    assert payload["result"]["stdout"].strip() == "done"


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
    tool = RunSkillScriptTool(reg_with_scripts)
    payload = json.loads(
        tool(
            skill_id="stdin-echo",
            args='{"skill_path":"/tmp/my-skill"}',
        )
    )
    assert payload["ok"] is True
    assert payload["script_ok"] is True
    assert payload["result"]["stdout"].strip() == "/tmp/my-skill"
