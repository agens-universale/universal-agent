"""TDD: finish-gate handoff form (spec 01-finish-gate-handoff-form).

Контракт turn_finish: `handoff` в аргументах — JSON-строка объекта.
В tool output / digest то же поле уже object. Валидатор не должен
требовать nested object в аргументах и не считать string↔object
противоречием Axis B.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from intent_guard.final_report_consistency import (
    _load_guard_prompt,
    validate_final_report_vs_user_intent,
)
from intent_guard.trace_digest import FinishGateTraceDigest
from tools.turn.submit_solution import SubmitSolution

_DIGEST_HANDOFF = {"artifacts": ["report.md"]}


def _validator_json(**overrides: object) -> str:
    base = {
        "action": "accept",
        "trace_ok": True,
        "report_ok": True,
        "trace_gaps": [],
        "report_mismatches": [],
    }
    base.update(overrides)
    return json.dumps(base)


def _ctx_state_digest(*, digest_handoff: dict | None = None):
    handoff = digest_handoff if digest_handoff is not None else _DIGEST_HANDOFF
    state = SimpleNamespace(messages=[{"role": "user", "content": "done?"}])
    ctx = SimpleNamespace(
        config_dict={},
        logger=None,
        chat_completions_llm=SimpleNamespace(
            max_tokens=8192,
            completion_kwargs={"reasoning_effort": "none"},
        ),
    )
    tool_out = json.dumps(
        {"ok": True, "handoff": handoff},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = FinishGateTraceDigest(
        trace_digest_text=(
            '<message n="1">\n[user] done?\n</message>\n'
            f'<message n="2">\n[tool turn_finish] {tool_out}\n</message>'
        ),
        digest_chars=200,
    )
    return ctx, state, digest


def _full_payload(mock_llm) -> dict:
    user_content = mock_llm.call_args.kwargs["messages"][1]["content"]
    marker = "Input:\n"
    assert marker in user_content
    return json.loads(user_content.split(marker, 1)[1])


def _finish_args_payload(mock_llm) -> dict:
    payload = _full_payload(mock_llm)
    assert isinstance(payload.get("finish_handoff_arguments"), dict)
    return payload["finish_handoff_arguments"]


# --- A1: нормализация handoff в args перед payload ----------------------------


class TestNormalizeHandoffInFinishArgs:
    """Юнит-тесты чистой функции `_normalize_handoff_in_finish_args` (спека §A1)."""

    def test_string_object_becomes_dict(self) -> None:
        """JSON-строка объекта в `handoff` парсится в dict; остальные поля args не трогаем."""
        from intent_guard.final_report_consistency import _normalize_handoff_in_finish_args

        args = {
            "solution_type": "full_solution",
            "task_report": "Done",
            "handoff": '{"artifacts":["report.md"],"note":"ok"}',
        }
        out = _normalize_handoff_in_finish_args(args)
        assert out["handoff"] == {"artifacts": ["report.md"], "note": "ok"}
        assert out["solution_type"] == "full_solution"
        assert out["task_report"] == "Done"

    def test_empty_object_string_is_valid(self) -> None:
        """`handoff: "{}"` — валидная пустая форма по схеме тула → становится `{}`."""
        from intent_guard.final_report_consistency import _normalize_handoff_in_finish_args

        out = _normalize_handoff_in_finish_args({"handoff": "{}"})
        assert out["handoff"] == {}

    def test_already_object_unchanged(self) -> None:
        """Если `handoff` уже dict (как в tool output) — оставляем как есть."""
        from intent_guard.final_report_consistency import _normalize_handoff_in_finish_args

        handoff = {"artifacts": ["a.md"]}
        out = _normalize_handoff_in_finish_args({"handoff": handoff})
        assert out["handoff"] == {"artifacts": ["a.md"]}

    def test_unparseable_string_left_as_is(self) -> None:
        """Битый JSON не парсим: строка уходит в payload как есть (валидатор может ругаться)."""
        from intent_guard.final_report_consistency import _normalize_handoff_in_finish_args

        raw = '{"artifacts": '
        out = _normalize_handoff_in_finish_args({"handoff": raw})
        assert out["handoff"] == raw

    def test_json_array_string_left_as_is(self) -> None:
        """JSON-массив — не object по контракту тула; не превращаем в dict, оставляем строку."""
        from intent_guard.final_report_consistency import _normalize_handoff_in_finish_args

        raw = '["not","an","object"]'
        out = _normalize_handoff_in_finish_args({"handoff": raw})
        assert out["handoff"] == raw

    def test_json_scalar_string_left_as_is(self) -> None:
        """Скаляры (string/number/null/bool) — не object; нормализация их не трогает."""
        from intent_guard.final_report_consistency import _normalize_handoff_in_finish_args

        for raw in ('"just a string"', "42", "null", "true"):
            out = _normalize_handoff_in_finish_args({"handoff": raw})
            assert out["handoff"] == raw

    def test_missing_handoff_unchanged(self) -> None:
        """Нет ключа `handoff` — args возвращаем без изменений, ключ не добавляем."""
        from intent_guard.final_report_consistency import _normalize_handoff_in_finish_args

        args = {"solution_type": "direct_answer", "task_report": "hi"}
        out = _normalize_handoff_in_finish_args(args)
        assert out == args
        assert "handoff" not in out

    def test_does_not_mutate_input(self) -> None:
        """Функция не мутирует входной dict: исходный `handoff` остаётся строкой."""
        from intent_guard.final_report_consistency import _normalize_handoff_in_finish_args

        args = {"handoff": '{"x":1}'}
        out = _normalize_handoff_in_finish_args(args)
        assert args["handoff"] == '{"x":1}'
        assert out["handoff"] == {"x": 1}
        assert out is not args


# --- A1: payload валидатора получает object, не string ------------------------


class TestValidatorPayloadHandoffNormalized:
    """Интеграция: `validate_final_report_vs_user_intent` кладёт в LLM-payload object."""

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_string_handoff_in_args_becomes_object_in_payload(self, mock_llm) -> None:
        """В payload к LLM `handoff` уже dict, а не JSON-строка — нет ложного string vs object."""
        mock_llm.return_value = _validator_json()
        ctx, state, digest = _ctx_state_digest()
        validate_final_report_vs_user_intent(
            ctx=ctx,
            state=state,
            current_user_requests=["done?"],
            finish_handoff_arguments={
                "solution_type": "full_solution",
                "task_report": "Wrote report.md",
                "handoff": '{"artifacts":["report.md"]}',
            },
            trace_digest=digest,
        )
        finish_args = _finish_args_payload(mock_llm)
        assert isinstance(finish_args["handoff"], dict)
        assert finish_args["handoff"] == {"artifacts": ["report.md"]}

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_empty_object_string_in_payload(self, mock_llm) -> None:
        """Пустой handoff `"{}"` в аргументах доходит до LLM как пустой object `{}`."""
        mock_llm.return_value = _validator_json()
        ctx, state, digest = _ctx_state_digest()
        validate_final_report_vs_user_intent(
            ctx=ctx,
            state=state,
            current_user_requests=["done?"],
            finish_handoff_arguments={
                "solution_type": "direct_answer",
                "task_report": "Answered.",
                "handoff": "{}",
            },
            trace_digest=digest,
        )
        finish_args = _finish_args_payload(mock_llm)
        assert finish_args["handoff"] == {}

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_args_as_json_string_with_string_handoff(self, mock_llm) -> None:
        """Сырые args тула одной JSON-строкой: внутри handoff-строка → в payload всё равно dict."""
        mock_llm.return_value = _validator_json()
        ctx, state, digest = _ctx_state_digest()
        raw_args = json.dumps(
            {
                "solution_type": "full_solution",
                "task_report": "Done",
                "handoff": '{"artifacts":["out.txt"]}',
            },
            ensure_ascii=False,
        )
        validate_final_report_vs_user_intent(
            ctx=ctx,
            state=state,
            current_user_requests=["done?"],
            finish_handoff_arguments=raw_args,
            trace_digest=digest,
        )
        finish_args = _finish_args_payload(mock_llm)
        assert finish_args["handoff"] == {"artifacts": ["out.txt"]}

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_object_handoff_still_object_in_payload(self, mock_llm) -> None:
        """Если handoff уже пришёл object — в payload остаётся тот же object (идемпотентность)."""
        mock_llm.return_value = _validator_json()
        ctx, state, digest = _ctx_state_digest()
        validate_final_report_vs_user_intent(
            ctx=ctx,
            state=state,
            current_user_requests=["done?"],
            finish_handoff_arguments={
                "solution_type": "full_solution",
                "task_report": "Done",
                "handoff": {"artifacts": ["report.md"]},
            },
            trace_digest=digest,
        )
        finish_args = _finish_args_payload(mock_llm)
        assert finish_args["handoff"] == {"artifacts": ["report.md"]}

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_broken_handoff_string_left_for_validator(self, mock_llm) -> None:
        """Битый handoff не «чиним»: в payload уходит исходная строка для Axis B."""
        mock_llm.return_value = _validator_json()
        ctx, state, digest = _ctx_state_digest()
        broken = '{"artifacts":'
        validate_final_report_vs_user_intent(
            ctx=ctx,
            state=state,
            current_user_requests=["done?"],
            finish_handoff_arguments={
                "solution_type": "full_solution",
                "task_report": "Done",
                "handoff": broken,
            },
            trace_digest=digest,
        )
        finish_args = _finish_args_payload(mock_llm)
        assert finish_args["handoff"] == broken


# --- Приёмка 1 / Цель 2: string в args ≡ object в digest ----------------------


class TestStringVsObjectEquivalence:
    """Одинаковое содержимое, разная форма → после A1 валидатор видит object↔object."""

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_payload_does_not_expose_handoff_as_json_string(self, mock_llm) -> None:
        """В `finish_handoff_arguments` поле `handoff` — nested object, не JSON-строка."""
        mock_llm.return_value = _validator_json()
        ctx, state, digest = _ctx_state_digest()
        validate_final_report_vs_user_intent(
            ctx=ctx,
            state=state,
            current_user_requests=["done?"],
            finish_handoff_arguments={
                "solution_type": "full_solution",
                "task_report": "Wrote report.md",
                "handoff": '{"artifacts":["report.md"]}',
            },
            trace_digest=digest,
        )
        finish_args = _finish_args_payload(mock_llm)
        assert isinstance(finish_args["handoff"], dict)
        assert finish_args["handoff"]["artifacts"] == ["report.md"]
        # В сериализованном блоке args handoff — object (`"handoff": {`), не строка
        args_block = json.dumps(finish_args, ensure_ascii=False, indent=2)
        assert '"handoff": {' in args_block
        assert '"handoff": "{' not in args_block

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_normalized_args_handoff_matches_digest_object_content(
        self, mock_llm
    ) -> None:
        """Цель 2: после нормализации args.handoff == содержимому object в digest (эквивалентность форм)."""
        mock_llm.return_value = _validator_json()
        ctx, state, digest = _ctx_state_digest(digest_handoff=_DIGEST_HANDOFF)
        validate_final_report_vs_user_intent(
            ctx=ctx,
            state=state,
            current_user_requests=["done?"],
            finish_handoff_arguments={
                "solution_type": "full_solution",
                "task_report": "Wrote report.md",
                "handoff": json.dumps(_DIGEST_HANDOFF, ensure_ascii=False),
            },
            trace_digest=digest,
        )
        payload = _full_payload(mock_llm)
        args_handoff = payload["finish_handoff_arguments"]["handoff"]
        assert isinstance(args_handoff, dict)
        assert args_handoff == _DIGEST_HANDOFF
        digest_handoff_json = json.dumps(
            _DIGEST_HANDOFF, ensure_ascii=False, separators=(",", ":")
        )
        assert digest_handoff_json in payload["trace_digest_text"].replace(" ", "")


# --- Приёмка 2: расхождение содержимого по-прежнему видно и отклоняется --------


class TestContentMismatchStillRejected:
    """Фикс формы не должен ломать Axis B: content-mismatch всё ещё виден и reject'ится."""

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_content_divergence_preserved_in_normalized_payload(
        self, mock_llm
    ) -> None:
        """A1 не подменяет содержимое: wrong.md в args остаётся в payload, digest держит report.md."""
        mock_llm.return_value = _validator_json()
        ctx, state, digest = _ctx_state_digest(
            digest_handoff={"artifacts": ["report.md"]}
        )
        validate_final_report_vs_user_intent(
            ctx=ctx,
            state=state,
            current_user_requests=["done?"],
            finish_handoff_arguments={
                "solution_type": "full_solution",
                "task_report": "Wrote wrong.md",
                "handoff": '{"artifacts":["wrong.md"]}',
            },
            trace_digest=digest,
        )
        finish_args = _finish_args_payload(mock_llm)
        assert finish_args["handoff"] == {"artifacts": ["wrong.md"]}
        payload = _full_payload(mock_llm)
        assert "report.md" in payload["trace_digest_text"]
        assert finish_args["handoff"] != {"artifacts": ["report.md"]}

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_llm_content_mismatch_still_rejects(self, mock_llm) -> None:
        """Приёмка 2: если LLM нашёл расхождение содержимого — результат reject (`fix_report`), не accept."""
        mock_llm.return_value = _validator_json(
            action="fix_report",
            trace_ok=True,
            report_ok=False,
            report_mismatches=[
                "handoff.artifacts claims wrong.md but digest shows report.md"
            ],
        )
        ctx, state, digest = _ctx_state_digest(
            digest_handoff={"artifacts": ["report.md"]}
        )
        result = validate_final_report_vs_user_intent(
            ctx=ctx,
            state=state,
            current_user_requests=["done?"],
            finish_handoff_arguments={
                "solution_type": "full_solution",
                "task_report": "Wrote wrong.md",
                "handoff": '{"artifacts":["wrong.md"]}',
            },
            trace_digest=digest,
        )
        assert result.ok is False
        assert result.action == "fix_report"
        assert result.report_ok is False
        assert any("wrong.md" in m or "artifacts" in m for m in result.report_mismatches)

    @patch("intent_guard.final_report_consistency.request_chat_completion_text")
    def test_form_only_mismatch_claim_is_not_forced_by_payload(
        self, mock_llm
    ) -> None:
        """Приёмка 1: при корректном string-handoff payload уже object — нет повода для form-only mismatch."""
        mock_llm.return_value = _validator_json()
        ctx, state, digest = _ctx_state_digest()
        validate_final_report_vs_user_intent(
            ctx=ctx,
            state=state,
            current_user_requests=["done?"],
            finish_handoff_arguments={
                "solution_type": "full_solution",
                "task_report": "Wrote report.md",
                "handoff": '{"artifacts":["report.md"]}',
            },
            trace_digest=digest,
        )
        finish_args = _finish_args_payload(mock_llm)
        assert not isinstance(finish_args["handoff"], str)
        assert finish_args["handoff"] == _DIGEST_HANDOFF


# --- Non-goal: схема turn_finish остаётся string ------------------------------


def test_turn_finish_handoff_schema_stays_string():
    """Non-goal спеки: схема `turn_finish.handoff` остаётся `type: string`, не nested object."""
    handoff = SubmitSolution.tool_description()["parameters"]["properties"]["handoff"]
    assert handoff["type"] == "string"


# --- A2: оговорка в промпте гейта ---------------------------------------------


def test_guard_prompt_string_vs_object_handoff_rule():
    """A2: в промпте гейта явно сказано не считать string↔object за `report_mismatches`."""
    prompt = _load_guard_prompt()
    lower = prompt.lower()
    assert "arguments" in lower
    assert "handoff" in lower
    assert "report_mismatches" in prompt
    assert (
        "string-vs-object" in lower
        or "string vs object" in lower
        or "string-encoded" in lower
        or "json string encoding an object" in lower
    )
    assert "do not treat" in lower or "not treat" in lower or "do **not** treat" in lower
    assert "decoded content" in lower or "content contradicts" in lower


def test_guard_prompt_still_rejects_content_contradictions():
    """A2 не отменяет Axis B: промпт по-прежнему требует reject при противоречии содержимого handoff."""
    prompt = _load_guard_prompt()
    assert "handoff.artifacts" in prompt
    assert "report_mismatches" in prompt
    assert "contradict" in prompt.lower() or "contradiction" in prompt.lower()
