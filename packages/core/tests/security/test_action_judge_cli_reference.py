"""Эталон запроса судьи действий из CLI и TUI (спека §3.7).

Слова пользователя здесь свои: вид записи `external` и правило о внешнем
запросе запрос не меняют ни на байт. Сравниваются полные сообщения судье —
с инструкциями, прочитанными локально, и с принесёнными пробой воркера.
Эталон меняют только правки, меняющие сам запрос CLI и TUI, — вместе с
объяснением в описании PR.
"""

import json
from pathlib import Path
from types import SimpleNamespace

from gc_testkit.settings import fake_ctx

from gc_core.execution.guard_contracts import GuardProbeRequest
from gc_core.intent_log.record import IntentRecord
from gc_core.security.action_judge import (
    build_action_judge_messages,
    build_action_judge_payload,
)
from gc_core.security.action_judge_input import build_judge_request
from gc_core.security.guard_probe import evaluate_guard_probe

REFERENCE = Path(__file__).parent / "data" / "action_judge_cli_reference.json"

_TOOLS = {
    "exec_command": {"category": "exec"},
    "fs_read_file": {"category": "read"},
    "turn_ask_human": {"category": "turn"},
}


def _call(name, args, call_id):
    return {
        "type": "function_call",
        "name": name,
        "arguments": json.dumps(args, ensure_ascii=False),
        "call_id": call_id,
    }


def _out(call_id, payload):
    output = (
        payload
        if isinstance(payload, str)
        else json.dumps(payload, ensure_ascii=False)
    )
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }


def _state():
    """Длинная сессия CLI после сжатия: ранние слова — только в журнале
    намерений; в переписке — длинное сообщение (режется по середине),
    чтение (судья его не видит), прежнее действие с ошибкой, вопрос агента
    с ответом человека и проверяемая команда с длинными аргументами."""
    return SimpleNamespace(
        messages=[
            {"role": "system", "content": "системный промпт"},
            {"role": "user", "content": "почини тесты оплаты. " * 1_500},
            _call("fs_read_file", {"path": "pay.py"}, "c1"),
            _out("c1", "секрет из файла"),
            _call("exec_command", {"command": "pytest -q tests/pay"}, "c2"),
            _out(
                "c2",
                {
                    "tool": "exec_command",
                    "ok": False,
                    "error": {"type": "exit_code"},
                },
            ),
            _call("turn_ask_human", {"question": "Запушить в fix?"}, "c3"),
            _out(
                "c3",
                {"tool": "turn_ask_human", "ok": True, "response": "да"},
            ),
            _call(
                "exec_command",
                {"command": "git push origin fix " + "x" * 20_000},
                "judged",
            ),
        ],
        user_intent_log=[
            IntentRecord(1, "ts", "user_ask", None, "не трогай ветку main")
        ],
        current_user_intent=[],
        shared=SimpleNamespace(plan=None),
        parent_state_id=None,
    )


def reference_messages() -> dict:
    """Сообщения судье для представительного запроса CLI/TUI: с
    инструкциями из файлов и с инструкциями из пробы воркера."""
    state = _state()
    request = build_judge_request(
        state,
        SimpleNamespace(),
        tool="exec_command",
        category="exec",
        arguments=json.loads(state.messages[-1]["arguments"]),
        call_id="judged",
        tools_data=_TOOLS,
        flags=("outside_project",),
        runtime={"project_dir": "/p", "os": "linux"},
    )
    payload = build_action_judge_payload(request)
    facts = evaluate_guard_probe(
        GuardProbeRequest("action_prompts", "exec_command", "", "/p"),
        settings=fake_ctx(),
    )
    return {
        "local": build_action_judge_messages(payload),
        "probe": build_action_judge_messages(payload, prompts=facts.data),
    }


def test_cli_and_tui_judge_request_matches_the_reference():
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))
    assert reference_messages() == {"local": reference, "probe": reference}


def test_the_reference_covers_what_long_cli_sessions_send():
    """Эталон не пустышка: в нём журнал после сжатия, ответ человека,
    обрезка сообщения и аргументов, пометка и факты окружения."""
    user = json.loads(REFERENCE.read_text(encoding="utf-8"))[1]["content"]
    assert "не трогай ветку main" in user
    assert '"user": "да"' in user
    assert "[TEXT CUTTED]" in user
    assert '"arguments_truncated": true' in user
    assert '"outside_project"' in user
    assert '"project_dir": "/p"' in user
    assert "секрет из файла" not in user
