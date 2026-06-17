from run_context import RunContext, create_run_context


def test_runcontext_has_environment_fields_default_none():
    fields = RunContext.__dataclass_fields__
    assert "environment" in fields
    assert "harbor_backend" in fields


def test_create_run_context_accepts_environment():
    sentinel_env = object()
    cfg = {"backend_api_mode": "chat_completions", "save_logs": False}
    ctx = create_run_context(cfg, environment=sentinel_env)
    assert ctx.environment is sentinel_env
    assert ctx.harbor_backend is None
