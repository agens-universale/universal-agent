from run_context import RunContext, create_run_context

from tools.backends.local import LocalFsBackend


def test_runcontext_has_environment_and_os_backend_fields():
    fields = RunContext.__dataclass_fields__
    assert "environment" in fields
    assert "os_backend" in fields
    assert "harbor_backend" not in fields


def test_create_run_context_accepts_environment_and_builds_backend():
    sentinel_env = object()
    cfg = {"backend_api_mode": "chat_completions", "save_logs": False}
    ctx = create_run_context(cfg, environment=sentinel_env)
    assert ctx.environment is sentinel_env
    assert isinstance(ctx.os_backend, LocalFsBackend)
