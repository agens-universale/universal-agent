from states import State
from tests.test_project_data_backend import FakeSandboxBackend


def _state(**kwargs) -> State:
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
    return State(**base)


def test_state_without_backend_uses_local_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = _state()
    assert state.initial_project_data["projectDir"] == str(tmp_path.resolve())


def test_state_with_sandbox_backend_uses_container_cwd():
    backend = FakeSandboxBackend(cwd="/testbed", tree={"/testbed": [("pkg", True)]}, git=True)
    state = _state(os_backend=backend)
    assert state.initial_project_data["projectDir"] == "/testbed"
    assert state.initial_project_data["isGitProject"] == "true"


def test_state_with_backend_is_json_serializable():
    """The backend is a live I/O handle: it must not leak into the persisted snapshot."""
    import json

    from persistence.state_repository import StateRepository

    backend = FakeSandboxBackend(cwd="/testbed", tree={"/testbed": []}, git=False)
    state = _state(os_backend=backend)

    payload = StateRepository.serialize_storage({state.state_id: state})

    json.dumps(payload)                                   # must not raise
    assert "os_backend" not in payload[state.state_id]
