"""State.discard_writes_after_stop — флаг для guard перед persist после стопа."""
from gc_core.states import State


def _make_state() -> State:
    return State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=2,
        system_prompt_id="v1",
        use_server_subagents=False,
    )


def test_state_discard_writes_after_stop_defaults_false():
    """Дефолт False — zero regression для CLI/server/swarm."""
    state = _make_state()
    assert state.discard_writes_after_stop is False


def test_state_discard_writes_after_stop_can_be_set_true():
    """TUI-путь выставляет True."""
    state = _make_state()
    state.discard_writes_after_stop = True
    assert state.discard_writes_after_stop is True


async def test_persist_callback_skipped_when_discard_and_stop():
    """Если discard_writes_after_stop=True И stop_requested=True — callback не вызывается."""
    state = _make_state()
    state.discard_writes_after_stop = True
    state.stop_requested = True

    persisted: list = []
    def callback(messages: list) -> None:
        persisted.append(messages)

    from gc_core.processing_responses import _maybe_persist
    await _maybe_persist(callback, [{"role": "user", "content": "x"}], state)

    assert persisted == []


async def test_persist_callback_called_when_not_discarding():
    """Если discard_writes_after_stop=False (default) — callback вызывается как обычно даже на stop."""
    state = _make_state()
    state.stop_requested = True  # стоп есть, но discard False (CLI/server поведение)

    persisted: list = []
    def callback(messages: list) -> None:
        persisted.append(messages)

    from gc_core.processing_responses import _maybe_persist
    await _maybe_persist(callback, [{"role": "user", "content": "x"}], state)

    assert len(persisted) == 1


async def test_persist_callback_called_when_no_stop():
    """Если stop_requested=False — callback всегда вызывается."""
    state = _make_state()
    state.discard_writes_after_stop = True  # флаг True, но стопа нет

    persisted: list = []
    def callback(messages: list) -> None:
        persisted.append(messages)

    from gc_core.processing_responses import _maybe_persist
    await _maybe_persist(callback, [{"role": "user", "content": "x"}], state)

    assert len(persisted) == 1


async def test_persist_callback_none_is_noop():
    """callback=None не падает."""
    state = _make_state()
    # Не должно бросать
    from gc_core.processing_responses import _maybe_persist
    await _maybe_persist(None, [], state)


def _make_executor_cfg(tmp_path):
    return {
        "threads_data_path": str(tmp_path),
        "backend_api_mode": "chat_completions",
        "completions_url": "http://test",
        "model": "x",
        "max_steps": 1,
        "system_prompt_id": "v1",
    }


async def test_run_kwarg_sets_state_field(tmp_path, monkeypatch):
    """executor.run(discard_writes_after_stop=True) выставляет state.discard_writes_after_stop=True."""
    from gc_core.agent_executor import AgentExecutor
    from gc_core.run_context import acreate_run_context

    cfg = _make_executor_cfg(tmp_path)

    # Monkey-patch run_agent to immediately exit без HTTP
    async def _fake_arun_agent(*a, **kw):
        return {"finished": True}

    monkeypatch.setattr("gc_core.agent_executor.arun_agent", _fake_arun_agent)
    ctx = await acreate_run_context(cfg, auth_connector=None)
    ex = AgentExecutor(ctx)
    await ex.arun([{"role": "user", "content": "x"}],
                  thread_id="t", start_new_thread=True, system="cli",
                  discard_writes_after_stop=True)
    assert ex._state.discard_writes_after_stop is True


async def test_run_default_kwarg_leaves_state_false(tmp_path, monkeypatch):
    """Без kwarg — state.discard_writes_after_stop остаётся False."""
    from gc_core.agent_executor import AgentExecutor
    from gc_core.run_context import acreate_run_context

    cfg = _make_executor_cfg(tmp_path)

    async def _fake_arun_agent(*a, **kw):
        return {"finished": True}

    monkeypatch.setattr("gc_core.agent_executor.arun_agent", _fake_arun_agent)
    ctx = await acreate_run_context(cfg, auth_connector=None)
    ex = AgentExecutor(ctx)
    await ex.arun([{"role": "user", "content": "x"}],
                  thread_id="t", start_new_thread=True, system="cli")
    assert ex._state.discard_writes_after_stop is False
