import unittest
from types import SimpleNamespace

from gc_core.config_modules.quality_gate import (
    DEFAULT_QUALITY_GATE_MAX_DIALOGUE_CHARS,
    DEFAULT_QUALITY_GATE_MAX_MESSAGE_CHARS,
    QualityGateConfig,
    normalize_quality_gate_config,
)
from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_core.intent_guard.trace_digest import build_finish_gate_trace_digest
from gc_core.message_format import dialog_history


def _ask_human_scenario_messages(
    *, include_pending_submit: bool = True
) -> list[dict]:
    """Сценарий §1.2: «спроси меня» → turn_ask_human → ответ → fs_write → turn_finish."""
    messages: list[dict] = [
        {"role": "user", "content": "предыдущий запрос в треде"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "спроси меня что нибудь"},
        {
            "type": "function_call",
            "call_id": "ah1",
            "name": "turn_ask_human",
            "arguments": {"question": "Какой навык хочешь прокачать?"},
        },
        {
            "type": "function_call_output",
            "call_id": "ah1",
            "output": {
                "tool": "turn_ask_human",
                "ok": True,
                "question": "Какой навык хочешь прокачать?",
                "response": "как осанку исправить",
            },
        },
        {
            "type": "function_call",
            "call_id": "wf1",
            "name": "fs_write_file",
            "arguments": {"path": "posture_plan.md", "content": "plan"},
        },
        {
            "type": "function_call_output",
            "call_id": "wf1",
            "output": {
                "tool": "fs_write_file",
                "ok": True,
                "path": "posture_plan.md",
            },
        },
    ]
    if include_pending_submit:
        messages.append(
            {
                "type": "function_call",
                "call_id": "ss1",
                "name": "turn_finish",
                "arguments": {
                    "solution_type": "full_solution",
                    "task_report": "done",
                    "handoff": {"artifacts": ["posture_plan.md"]},
                },
            }
        )
    return messages


class FinishGateTraceDigestTest(unittest.TestCase):
    def test_normalize_quality_gate_config_clamps_invalid_values(self) -> None:
        qg = QualityGateConfig(max_dialogue_chars=0, max_message_chars=-1)
        qg = normalize_quality_gate_config(qg)
        self.assertEqual(
            qg.max_dialogue_chars, DEFAULT_QUALITY_GATE_MAX_DIALOGUE_CHARS
        )
        self.assertEqual(
            qg.max_message_chars, DEFAULT_QUALITY_GATE_MAX_MESSAGE_CHARS
        )

    def test_resolve_paths_normalizes_quality_gate_in_config_dict(
        self,
    ) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                quality_gate=QualityGateConfig(
                    max_dialogue_chars=0, max_message_chars=-1
                )
            )
            cfg = resolve_paths(cfg, tmp)
            self.assertEqual(
                cfg.quality_gate.max_dialogue_chars,
                DEFAULT_QUALITY_GATE_MAX_DIALOGUE_CHARS,
            )
            self.assertEqual(
                cfg.quality_gate.max_message_chars,
                DEFAULT_QUALITY_GATE_MAX_MESSAGE_CHARS,
            )

    def test_trace_digest_contains_ask_human_and_response(self) -> None:
        state = SimpleNamespace(messages=_ask_human_scenario_messages())
        digest = build_finish_gate_trace_digest(state=state)
        text = digest.trace_digest_text
        self.assertIn("[function_call] turn_ask_human", text)
        self.assertIn("как осанку исправить", text)
        self.assertIn("спроси меня что нибудь", text)

    def test_trace_digest_includes_messages_before_current_cli_request(
        self,
    ) -> None:
        state = SimpleNamespace(messages=_ask_human_scenario_messages())
        digest = build_finish_gate_trace_digest(state=state)
        self.assertIn("предыдущий запрос в треде", digest.trace_digest_text)

    def test_pending_submit_solution_excluded_from_digest(self) -> None:
        state = SimpleNamespace(messages=_ask_human_scenario_messages())
        digest = build_finish_gate_trace_digest(state=state)
        text = digest.trace_digest_text
        self.assertNotIn("[function_call] turn_finish", text)
        self.assertNotIn("full_solution", text)

    def test_pending_submit_solution_output_excluded_from_digest(self) -> None:
        messages = _ask_human_scenario_messages()
        messages.append(
            {
                "type": "function_call_output",
                "call_id": "ss1",
                "output": {
                    "tool": "turn_finish",
                    "ok": True,
                    "status": "submitted",
                    "task_report": "duplicate pending report",
                },
            }
        )
        state = SimpleNamespace(messages=messages)
        digest = build_finish_gate_trace_digest(state=state)
        text = digest.trace_digest_text
        self.assertNotIn("[function_call] turn_finish", text)
        self.assertNotIn("duplicate pending report", text)

    def test_previous_submit_solution_remains_when_not_last(self) -> None:
        messages = _ask_human_scenario_messages(include_pending_submit=False)
        messages.extend(
            [
                {
                    "type": "function_call",
                    "call_id": "ss_old",
                    "name": "turn_finish",
                    "arguments": {
                        "solution_type": "direct_answer",
                        "task_report": "old",
                    },
                },
                {"role": "user", "content": "technical feedback after reject"},
                {
                    "type": "function_call",
                    "call_id": "ss_new",
                    "name": "turn_finish",
                    "arguments": {
                        "solution_type": "full_solution",
                        "task_report": "new",
                    },
                },
            ]
        )
        state = SimpleNamespace(messages=messages)
        digest = build_finish_gate_trace_digest(state=state)
        text = digest.trace_digest_text
        self.assertNotIn('"task_report": "new"', text)
        self.assertNotIn("full_solution", text)
        self.assertIn("[function_call] turn_finish", text)
        self.assertIn("direct_answer", text)

    def test_trace_digest_omits_reasoning_field(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "visible",
                "reasoning_content": "SECRET_CHAIN",
            },
            {
                "role": "assistant",
                "content": "visible2",
                "reasoning": "ALIAS_CHAIN",
            },
        ]
        digest = build_finish_gate_trace_digest(
            state=SimpleNamespace(messages=messages),
            exclude_pending_turn_finish=False,
        )
        text = digest.trace_digest_text
        self.assertNotIn("SECRET_CHAIN", text)
        self.assertNotIn("ALIAS_CHAIN", text)
        self.assertNotIn("[reasoning]", text)
        self.assertIn("visible", text)
        self.assertIn("visible2", text)

    def test_long_reasoning_does_not_evict_early_tool_receipt(self) -> None:
        messages: list[dict] = [
            {
                "type": "function_call",
                "call_id": "w1",
                "name": "fs_write_file",
                "arguments": {"path": "a.md", "content": "x"},
            },
            {
                "type": "function_call_output",
                "call_id": "w1",
                "output": {
                    "tool": "fs_write_file",
                    "ok": True,
                    "r": ("RECEIPT_EARLY"),
                },
            },
        ]
        messages += [
            {
                "role": "assistant",
                "content": f"step {n}",
                "reasoning_content": "r" * 8000,
            }
            for n in range(30)
        ]
        settings = AppConfig(
            quality_gate=QualityGateConfig(max_dialogue_chars=20000)
        )
        digest = build_finish_gate_trace_digest(
            state=SimpleNamespace(messages=messages),
            settings=settings,
            exclude_pending_turn_finish=False,
        )
        self.assertIn("RECEIPT_EARLY", digest.trace_digest_text)

    def test_reasoning_only_message_keeps_its_slot(self) -> None:
        messages = [
            {"role": "assistant", "content": "", "reasoning_content": "SECRET"}
        ]
        digest = build_finish_gate_trace_digest(
            state=SimpleNamespace(messages=messages),
            exclude_pending_turn_finish=False,
        )
        self.assertNotIn("SECRET", digest.trace_digest_text)
        self.assertIn("[assistant]", digest.trace_digest_text)

    def test_trace_digest_strips_inline_think(self) -> None:
        messages = [
            {"role": "assistant", "content": "<think>HIDDEN</think>visible"}
        ]
        digest = build_finish_gate_trace_digest(
            state=SimpleNamespace(messages=messages),
            exclude_pending_turn_finish=False,
        )
        self.assertNotIn("HIDDEN", digest.trace_digest_text)

    def test_build_history_text_matches_summarizer_pipeline(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "fs_read_file",
                "arguments": {"path": "/tmp/a.txt"},
            },
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": {"ok": True},
            },
            {"role": "assistant", "content": "done"},
        ]
        limits = {
            "max_dialogue_chars": DEFAULT_QUALITY_GATE_MAX_DIALOGUE_CHARS,
            "max_message_chars": DEFAULT_QUALITY_GATE_MAX_MESSAGE_CHARS,
        }
        trimmed = dialog_history.trim_history_to_max_dialogue_chars(
            messages,
            max_dialogue_chars=limits["max_dialogue_chars"],
            max_message_chars=limits["max_message_chars"],
        )
        expected = dialog_history.build_history_text(
            trimmed,
            max_chars=0,
            max_message_chars=limits["max_message_chars"],
        )
        state = SimpleNamespace(messages=messages)
        digest = build_finish_gate_trace_digest(
            state=state,
            exclude_pending_turn_finish=False,
        )
        self.assertEqual(digest.trace_digest_text, expected)
        self.assertEqual(digest.digest_chars, len(expected))


if __name__ == "__main__":
    unittest.main()
