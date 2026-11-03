import logging
import sys
import types

from gc_testkit import as_coro, run_coro
from gc_testkit.paths import module_dir
from gc_testkit.settings import fake_ctx
from helpers.module_snapshot import ModuleSnapshot

_SNAPSHOT: ModuleSnapshot | None = None


def teardown_module(_module) -> None:
    if _SNAPSHOT is not None:
        _SNAPSHOT.restore()


def _stub_tools_modules() -> None:
    for name in list(sys.modules):
        if name == "gc_core.tools" or name.startswith("gc_core.tools."):
            sys.modules.pop(name, None)

    tools_pkg = types.ModuleType("gc_core.tools")

    class _WrongFunctionArgumentsException(Exception):
        def to_llm_feedback(self):
            return str(self)

    async def _apply_tool(*_args, **_kwargs):
        raise AssertionError("apply_tool should not be called in this test")

    tools_pkg.aapply_tool = _apply_tool
    tools_pkg.WrongFunctionArgumentsException = (
        _WrongFunctionArgumentsException
    )
    tools_dir = module_dir("tools")
    # `processing_responses` импортирует эти имена на уровне модуля. Без них
    # подмена пакета даёт ImportError, и файл падает целиком — но только
    # когда реальные модули не подтянул сосед по прогону. При случайном
    # порядке (pytest-randomly) это молчащая мина, а не свойство файла.
    tools_pkg.is_internal_tool_error_output = lambda *_a, **_kw: False
    tools_pkg.is_unknown_tool_output = lambda *_a, **_kw: False
    tools_pkg.__path__ = [str(tools_dir)]
    sys.modules["gc_core.tools"] = tools_pkg


processing_module = None
State = None


def setup_module(_module) -> None:
    global processing_module, State
    global _SNAPSHOT
    _SNAPSHOT = ModuleSnapshot()
    _stub_tools_modules()
    from gc_core import processing_responses as proc
    from gc_core.states import State as St

    processing_module = proc
    proc.aapply_tool = sys.modules["gc_core.tools"].aapply_tool
    State = St


def test_chat_completions_logs_raw_backend_payload_on_conversion_error(
    caplog, monkeypatch
):
    def _fake_create_call_headers(_state, _ctx, resolved=None):
        return {"Authorization": "Bearer fake"}

    def _fake_create_call_data(_messages, _state, **_kwargs):
        return {
            "model": "CodeChat",
            "messages": [{"role": "user", "content": "ping"}],
            "tools": [],
        }

    async def _fake_request_policy(*_args, **_kwargs):
        # Нет поля choices -> внутри конвертации будет KeyError('choices').
        return (
            {"id": "chat_1", "object": "chat.completion", "model": "CodeChat"},
            {
                "model": "CodeChat",
                "messages": [{"role": "user", "content": "ping"}],
                "tools": [],
            },
        )

    monkeypatch.setattr(
        processing_module,
        "acreate_call_headers",
        as_coro(_fake_create_call_headers),
    )
    monkeypatch.setattr(
        processing_module,
        "create_call_data_completions",
        _fake_create_call_data,
    )
    monkeypatch.setattr(
        processing_module,
        "arequest_chat_completions_with_policy",
        _fake_request_policy,
    )

    logger = logging.getLogger("test_backend_payload_logging")
    ctx = fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://backend.local/v1/chat/completions",
            "backend.use_ssl": True,
            "backend.use_retry": False,
            "backend.request_timeout_sec": 600,
        },
        logger=logger,
    )
    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=1,
        system_prompt_id="v1",
        use_server_subagents=False,
    )

    with caplog.at_level(logging.ERROR):
        try:
            run_coro(
                processing_module._arequest_backend_canonical(
                    [{"role": "user", "content": "hello", "id": "m_1"}],
                    state,
                    ctx,
                    resolved=None,
                )
            )
            assert False, "Expected KeyError('choices')"
        except KeyError as exc:
            assert str(exc) == "'choices'"

    log_text = caplog.text
    assert "Failed to convert chat/completions payload." in log_text
    assert "context=" in log_text
    assert "payload=" in log_text
    assert '"id": "chat_1"' in log_text
    assert '"object": "chat.completion"' in log_text
