from gc_core.execution_workspace import ExecutionWorkspace
from gc_core.states import State
from gc_core.utils.local_render_system_prompt import render_system_prompt
from test_project_data_backend import FakeSandboxBackend

TEMPLATE = "System rules.\n<user_info>\n</user_info>\n<specific_role>\n</specific_role>"


async def test_env_block_describes_container_not_runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                    # runner cwd — must NOT leak into the prompt
    backend = FakeSandboxBackend(
        cwd="/testbed",
        tree={"/testbed": [("django", True), ("setup.py", False)]},
        git=True,
    )
    state = await State.acreate(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="sp",
        use_server_subagents=False,
        workspace=ExecutionWorkspace(root="/testbed", backend=backend, shell="sh", os_name="Linux", root_writable=True),
    )

    prompt = render_system_prompt(
        TEMPLATE,
        tools_spec=[],
        project_info=state.initial_project_data,
        specific_role=None,
    )

    assert "<item>projectDir: /testbed</item>" in prompt
    assert "<item>isGitProject: true</item>" in prompt
    assert "django/" in prompt
    assert str(tmp_path) not in prompt
