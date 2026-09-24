from gc_core.run_context import RunContext, acreate_run_context

from gc_core.backends.local import LocalFsBackend


def test_runcontext_has_environment_and_os_backend_fields():
    fields = RunContext.__dataclass_fields__
    assert "environment" in fields
    assert "os_backend" in fields
    assert "workspace" in fields
    assert "harbor_backend" not in fields


async def test_create_run_context_accepts_environment_and_builds_backend(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sentinel_env = object()
    cfg = {"backend_api_mode": "chat_completions", "save_logs": False}
    ctx = await acreate_run_context(cfg, environment=sentinel_env)
    assert ctx.environment is sentinel_env
    # свой Local приходит обёрнутым трекером правок (журнал во всех режимах)
    assert isinstance(ctx.os_backend.inner, LocalFsBackend)
    assert ctx.workspace.backend is ctx.os_backend
    assert ctx.workspace.root == str(tmp_path.resolve())
