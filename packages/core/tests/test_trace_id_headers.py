from types import SimpleNamespace

from gc_core.utils.connect_backend import create_call_headers


class _StateStub:
    state_id = "state-1"
    x_target = None


def _ctx(**cfg):
    # trace_header_enabled=True: отправка трейса в модель — opt-in, без флага
    # заголовка не будет вовсе (для этого есть отдельный класс тестов ниже).
    base = {
        "x_cl_id": "12345",
        "backend_api_mode": "chat_completions",
        "trace_header_enabled": True,
    }
    base.update(cfg)
    return SimpleNamespace(config_dict=base, auth_connector=None)


class TestOldHeaderRemoved:
    def test_x_trace_id_is_gone_without_trace(self):
        headers = create_call_headers(_StateStub(), _ctx())
        assert "x_trace_id" not in headers

    def test_state_id_still_ships_as_session_id(self):
        headers = create_call_headers(_StateStub(), _ctx())
        assert headers["x-session-id"] == "state-1"


class TestTraceHeader:
    def test_default_name_carries_trace(self):
        headers = create_call_headers(_StateStub(), _ctx(trace_id="wf-42"))
        assert headers["x_trace_id"] == "wf-42"

    def test_custom_name_is_used(self):
        headers = create_call_headers(
            _StateStub(), _ctx(trace_id="wf-42", trace_id_header_name="X-Trace-Id")
        )
        assert headers["X-Trace-Id"] == "wf-42"
        assert "x_trace_id" not in headers

    def test_responses_mode_also_gets_the_header(self):
        headers = create_call_headers(
            _StateStub(), _ctx(trace_id="wf-42", backend_api_mode="responses")
        )
        assert headers["x_trace_id"] == "wf-42"

    def test_empty_trace_emits_nothing(self):
        headers = create_call_headers(_StateStub(), _ctx(trace_id="  "))
        assert "x_trace_id" not in headers

    def test_run_value_wins_over_static_config_header(self):
        headers = create_call_headers(
            _StateStub(),
            _ctx(
                trace_id="from-run",
                chat_completion_headers={"x_trace_id": "from-yaml"},
            ),
        )
        assert headers["x_trace_id"] == "from-run"

    def test_raw_value_is_sent_verbatim(self):
        headers = create_call_headers(_StateStub(), _ctx(trace_id="wf/123"))
        assert headers["x_trace_id"] == "wf/123"


class TestModelHeaderGate:
    """Отправка трейса в модель — opt-in через agent.trace_header_enabled."""

    def test_no_header_without_the_flag(self):
        ctx = SimpleNamespace(
            config_dict={
                "x_cl_id": "12345",
                "backend_api_mode": "chat_completions",
                "trace_id": "wf-42",
            },
            auth_connector=None,
        )
        headers = create_call_headers(_StateStub(), ctx)
        assert "x_trace_id" not in headers
        assert not [k for k in headers if "trace" in k.lower()]

    def test_flag_false_is_explicit_no(self):
        headers = create_call_headers(
            _StateStub(), _ctx(trace_id="wf-42", trace_header_enabled=False)
        )
        assert "x_trace_id" not in headers

    def test_flag_alone_without_trace_adds_nothing(self):
        headers = create_call_headers(_StateStub(), _ctx(trace_header_enabled=True))
        assert "x_trace_id" not in headers
