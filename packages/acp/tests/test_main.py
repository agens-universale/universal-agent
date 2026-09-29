"""Старт процесса: конфиг, harbor, тишина в stdout, ACP runtime defaults."""

from gc_acp.main import apply_acp_runtime_defaults, backend_rejection, main
from gc_acp.prompt_assets import acp_special_instructions_path


def test_apply_acp_runtime_defaults_allow_gate_off_todo_off():
    cfg = {}
    apply_acp_runtime_defaults(cfg)
    assert cfg["text_answer_mode"] == "allow"
    assert cfg["strict_turn_finish_validation"] is False
    assert cfg["finish_gate_smart"] is False
    assert cfg["todo_params"]["reminder_steps"] == 0
    assert cfg["chat_completions_special_instructions_path"] == (
        acp_special_instructions_path()
    )
    assert cfg["stop_tools"] == [
        "turn_ask_human",
        "turn_intermediate_reply",
    ]


def test_apply_acp_runtime_defaults_respects_overrides():
    cfg = {
        "acp_text_answer_mode": "judge",
        "acp_finish_gate_mode": "smart",
        "acp_todo_reminder_steps": 7,
    }
    apply_acp_runtime_defaults(cfg)
    assert cfg["text_answer_mode"] == "judge"
    assert cfg["strict_turn_finish_validation"] is True
    assert cfg["finish_gate_smart"] is True
    assert cfg["todo_params"]["reminder_steps"] == 7


def test_apply_acp_runtime_defaults_keeps_existing_stop_tools():
    cfg = {"stop_tools": ["project_memory", "turn_ask_human"]}
    apply_acp_runtime_defaults(cfg)
    assert cfg["stop_tools"] == [
        "project_memory",
        "turn_ask_human",
        "turn_intermediate_reply",
    ]


def test_acp_special_instructions_asset_exists():
    path = acp_special_instructions_path()
    with open(path, encoding="utf-8") as fd:
        text = fd.read()
    assert "Discussion vs implementation" in text
    assert "don't write code" in text.lower() or "Do not write" in text


def test_backend_rejection_names_harbor_and_accepts_local():
    assert backend_rejection({"os_backend": "local"}) is None
    assert backend_rejection({}) is None
    text = backend_rejection({"os_backend": "harbor"})
    assert text is not None
    assert "harbor" in text


def test_harbor_exits_before_protocol(monkeypatch, capsys):
    seen = {}

    def load(path, profile_override=None):
        seen["path"] = path
        seen["profile"] = profile_override
        return {"os_backend": "harbor"}, None

    def run(coro):
        coro.close()
        raise AssertionError("protocol started")

    monkeypatch.setattr("gc_acp.main.load_config_ex", load)
    monkeypatch.setattr("gc_acp.main.asyncio.run", run)
    assert main(["--config", "c.yml", "--profile", "work"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "harbor" in captured.err
    assert seen == {"path": "c.yml", "profile": "work"}


def test_unreadable_config_exits_before_protocol(monkeypatch, capsys):
    def load(path, profile_override=None):
        raise ValueError("сломан")

    def run(coro):
        coro.close()
        raise AssertionError("protocol started")

    monkeypatch.setattr("gc_acp.main.load_config_ex", load)
    monkeypatch.setattr(
        "gc_acp.main.explain_broken_config", lambda *args, **kwargs: "сломан"
    )
    monkeypatch.setattr("gc_acp.main.asyncio.run", run)
    assert main(["--config", "missing.yml"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "сломан" in captured.err


async def _no_lines():
    if False:
        yield ""


def test_local_config_is_silent_on_stdout(monkeypatch, capsys, tmp_path):
    def load(path, profile_override=None):
        return {
            "os_backend": "local",
            "threads_data_path": str(tmp_path),
        }, None

    monkeypatch.setattr("gc_acp.main.load_config_ex", load)
    monkeypatch.setattr("gc_acp.main.stdin_lines", _no_lines)
    assert main(["--config", "c.yml"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""


def test_local_start_applies_runtime_defaults(monkeypatch, tmp_path):
    held = {}

    def load(path, profile_override=None):
        return {
            "os_backend": "local",
            "threads_data_path": str(tmp_path),
        }, None

    async def serve(config):
        held["config"] = config

    monkeypatch.setattr("gc_acp.main.load_config_ex", load)
    monkeypatch.setattr("gc_acp.main.serve", serve)
    assert main(["--config", "c.yml"]) == 0
    cfg = held["config"]
    assert cfg["text_answer_mode"] == "allow"
    assert cfg["strict_turn_finish_validation"] is False
    assert cfg["finish_gate_smart"] is False
    assert cfg["todo_params"]["reminder_steps"] == 0
    assert cfg["chat_completions_special_instructions_path"].endswith(
        "chat_completions_acp_special_instructions.md"
    )
    assert "turn_ask_human" in cfg["stop_tools"]
    assert "turn_intermediate_reply" in cfg["stop_tools"]


def test_unsupported_backend_is_rejected():
    text = backend_rejection({"os_backend": "docker"})
    assert text is not None
    assert "docker" in text
