from gc_core.execution_workspace import ExecutionWorkspace
from gc_core.states import State
from test_project_data_backend import FakeSandboxBackend


async def _state(**kwargs) -> State:
    base = dict(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="sp",
        use_server_subagents=False,
    )
    base.update(kwargs)
    # Данные о проекте собирает фабрика: конструктор в файловую систему больше
    # не ходит — у порта одна форма, корутинная, а `__init__` ждать не умеет.
    return await State.acreate(**base)


async def test_state_without_backend_uses_local_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = await _state()
    assert state.initial_project_data["projectDir"] == str(tmp_path.resolve())


async def test_state_with_workspace_uses_container_root():
    backend = FakeSandboxBackend(
        cwd="/testbed", tree={"/testbed": [("pkg", True)]}, git=True
    )
    state = await _state(
        workspace=ExecutionWorkspace(
            root="/testbed",
            backend=backend,
            shell="sh",
            os_name="Linux",
            root_writable=True,
        )
    )
    assert state.initial_project_data["projectDir"] == "/testbed"
    assert state.initial_project_data["isGitProject"] == "true"
    assert state.workspace.root == "/testbed"
    assert state.workspace.shell == "sh"
    assert state.workspace.os_name == "Linux"
    assert state.workspace.root_writable is True
    assert state.workspace.backend is backend


async def test_state_os_backend_compat_builds_workspace():
    backend = FakeSandboxBackend(
        cwd="/testbed", tree={"/testbed": [("pkg", True)]}, git=True
    )
    state = await _state(os_backend=backend)
    assert state.workspace is not None
    assert state.workspace.root == "/testbed"
    assert state.workspace.backend is backend
    assert state.initial_project_data["projectDir"] == "/testbed"
