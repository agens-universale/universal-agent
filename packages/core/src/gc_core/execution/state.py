"""Serializable execution data. Construction of prompts/resources lives in State."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from gc_core.auto_routing.policy import Decision
from gc_core.auto_routing.state_ops import ROUTING_FIELDS, ensure_fields
from gc_core.execution.output_store import ToolCallOutputsStore
from gc_core.intent_log import deserialize_list, flush
from gc_core.intent_log.serialize import serialize_for_meta
from gc_core.usage.account import UsageAccount


@dataclass
class SharedStateData:
    """Данные, разделяемые между оркестратором и его субагентами."""

    # In-process + persist key ``working_memory`` (goal/constraints/state/knowledge).
    # Agent tool API: ``session_memory``; summary wire: ``<session_memory>`` inside
    # ``<persistent_state>``. Intentionally not renamed in JSON/yaml (thread compat).
    working_memory: Dict[str, str] = field(default_factory=dict)
    notes_md: str = ""
    fetch_url_cache: Dict[str, Any] = field(default_factory=dict)
    confirm_rules: Dict[str, bool] = field(default_factory=dict)
    # Чек-лист текущей задачи ОРКЕСТРАТОРА (тул ``session_todo``): [{content, status}],
    # status ∈ persistence.schema.TODO_STATUSES. Replace-семантика на каждый вызов.
    # Субагент ведёт свой — см. ``State.session_todo``: shared у них общий, и
    # replace-семантика затирала бы план родителя.
    session_todo: List[Dict[str, str]] = field(default_factory=list)
    # Режим разрешений, выбранный в сессии (Shift+Tab, флаг запуска). None —
    # выбора не было, действует `security.permissions.default_mode`. Субагент
    # наследует режим родителя: shared у них общий.
    permission_mode: Optional[str] = None
    # План режима plan: {text, status, approved_mode, updated_at}
    # (`gc_core.plan_store`). None — плана нет.
    plan: Optional[Dict[str, Any]] = None
    # Какой режим объявлен КОРНЕВОМУ агенту ("plan" | "auto" | None): здесь
    # отметка сохраняется в мете треда между прогонами. Читать и писать —
    # через `State.mode_announced`; у субагента отметка своя, в его State.
    mode_announced: Optional[str] = None
    # Человек убрал план (`/plan clear`): следующий прогон один раз сообщит об
    # этом модели — в истории и в сводке план ещё виден.
    plan_removed_notice: bool = False
    # Предохранитель судьи действий (auto) — как у эталона: 3 отказа
    # подряд или 20 за сессию ставят паузу; на паузе спорное действие решает
    # человек, а спросить некого — отказ без судьи. Счёт и пауза общие для
    # сессии и её субагентов (shared у них один) и снимаются только «да»
    # человека (`gc_core.security.action_judge_step`).
    judge_denials_in_a_row: int = 0
    judge_denials_total: int = 0
    judge_paused: bool = False


STATE_FIELDS = ROUTING_FIELDS + (
    "turn_messages_start",
    "agent_md_path",
    "agentsmd_override",
    "backend_api_mode",
    "call_id",
    "call_metadata",
    "chat_completions_system_prompt",
    "current_user_intent",
    "default_model",
    "discard_writes_after_stop",
    "dialog_steps_offset",
    "model_breaker",
    "finish_gate_rejection_count",
    "inbox_seq",
    "initial_project_data",
    "instruction_warnings",
    "internal_error_repeat_count",
    "is_tui",
    "last_backend_response_id",
    "last_batch_invalid_arguments_count",
    "last_batch_unknown_tool_count",
    "last_finish_reason",
    "last_internal_error_tool_name",
    "last_main_prompt_message_count",
    "last_main_prompt_tokens",
    "last_run_usage",
    "last_unknown_tool_name",
    "loop_detector_pending",
    "loop_detector_window",
    "max_steps",
    "messages",
    "mode",
    "parent_state_id",
    "plan_proposed",
    "judge_failures_in_a_row",
    "judge_stop_reason",
    "previous_response_id",
    "prepared_metadata",
    "project_memory_content_hash",
    "run_start_usage_meta",
    "shared",
    "skills_list",
    "skip_initial_user_ask_step_when_history",
    "specific_role",
    "state_id",
    "steps",
    "stop_requested",
    "subagent_mode_announced",
    "subagent_todo",
    "system_prompt_id",
    "terminal_outcome",
    "thread_id",
    "todo_create_nudge_sent",
    "todo_reminder_cnt",
    "tool_call_outputs_store",
    "tool_calls",
    "tool_usage_notice_cnt",
    "tools",
    "tools_descr",
    "turn_level",
    "turn_tool_calls",
    "unknown_tool_repeat_count",
    "usage",
    "use_server_subagents",
    "user_intent_log",
    "user_turn_seq",
    "verbose",
    "x_target",
    "_session_compact_batch_snapshot",
    "session_compact_applied",
    "allow_pairing_persist",
    "pending_tool_attachments",
    "vision_probe_turn",
)

#: Транзиентные кэши зрения: считаются на каждый запрос (спека изображений
#: §9) и в снимок не идут — после восстановления берут эти умолчания.
TRANSIENT_STATE_DEFAULTS = {
    "closing_reply": str,
    "vision_audience": lambda: None,
    "vision_live_ids": frozenset,
    "vision_request_limit": lambda: None,
    "vision_routes": dict,
    "vision_probe_notices": set,
    # Вопрос, на котором ход приостановлен (ask_human_suspends): его пишет
    # тул в ядре и уносит запись сессии; ребёнку и в activity не нужен.
    "pending_question": lambda: None,
}


class ExecutionState:
    @property
    def session_todo(self) -> List[Dict[str, str]]:
        """Чек-лист ТЕКУЩЕГО агента (тул ``session_todo``).

        Оркестратор ведёт его в ``shared`` — там он персистится и переживает
        компакцию. Субагент получает ``shared`` родителя по ссылке, а у тула
        replace-семантика, поэтому его чек-лист живёт в собственном поле: свой
        план ребёнка не должен затирать план родителя. Ребёнок умирает вместе
        с прогоном — персистить нечего.
        """
        if self.parent_state_id is not None:
            return self.subagent_todo
        return self.shared.session_todo

    @session_todo.setter
    def session_todo(self, items: List[Dict[str, str]]) -> None:
        if self.parent_state_id is not None:
            self.subagent_todo = items
        else:
            self.shared.session_todo = items

    @property
    def mode_announced(self) -> Optional[str]:
        """Какой режим ЭТОМУ агенту уже объявлен ("plan" | "auto" | None).

        Режим общий живой, а сообщение о нём каждый агент получает в свою
        историю — отметка своя у каждого. Корень хранит её в ``shared``: там
        она переживает прогоны в мете треда. Субагент делит ``shared`` с
        родителем, поэтому держит свою в собственном поле: его объявление не
        должно «съесть» объявление корня.
        """
        if self.parent_state_id is not None:
            return self.subagent_mode_announced
        return self.shared.mode_announced

    @mode_announced.setter
    def mode_announced(self, value: Optional[str]) -> None:
        if self.parent_state_id is not None:
            self.subagent_mode_announced = value
        else:
            self.shared.mode_announced = value

    def set_resp_headers_metadata(self, headers):
        x_target = headers.get("x-target")
        if x_target and self.x_target is None:
            self.x_target = x_target

    def add_message(self, message):
        self.messages.append(message)

    def mark_turn_start(self) -> None:
        if self.turn_messages_start is None:
            self.turn_messages_start = len(self.messages)

    def set_previous_response_id(self, previous_response_id):
        self.previous_response_id = previous_response_id

    def init_user_intent_log(self, entries: Any) -> None:
        """Initialize closed user turns from persisted thread meta."""
        self.user_intent_log = deserialize_list(entries)

    def init_current_user_intent(self, entries: Any) -> None:
        """Initialize open user turn from persisted thread meta."""
        self.current_user_intent = deserialize_list(entries)

    def init_user_turn_seq(self, seq: Any) -> None:
        """Initialize closed turn counter from persisted thread meta."""
        try:
            self.user_turn_seq = max(0, int(seq or 0))
        except (TypeError, ValueError):
            self.user_turn_seq = 0

    def infer_user_turn_seq_if_missing(self) -> None:
        """Legacy meta had no ``user_turn_seq``; infer minimal value from closed log."""
        if self.user_turn_seq > 0:
            return
        if not self.user_intent_log:
            return
        turns = [
            int(r.turn)
            for r in self.user_intent_log
            if isinstance(r.turn, int)
        ]
        self.user_turn_seq = max(turns) if turns else 1

    def init_subagent_tool_usage_notice(self) -> None:
        """Субагент: изолированный meta/state; wiki общая через shared (D34)."""
        self.project_memory_content_hash = None
        self.tool_usage_notice_cnt = {}
        self.todo_reminder_cnt = 0

    def flush_current_user_intent(self) -> None:
        """Flush open turn records into closed log and increment ``user_turn_seq``.

        Граница хода: сбрасываем ``turn_tool_calls`` — следующий ход считает вызовы
        заново (smart-гейт судьи, спека §3). Компакция границей хода не является и
        сюда не заходит, поэтому счётчик её переживает."""
        self.current_user_intent, self.user_intent_log, self.user_turn_seq = (
            flush(
                self.current_user_intent,
                self.user_intent_log,
                self.user_turn_seq,
            )
        )
        self.turn_tool_calls = 0

    def to_execution_snapshot(self) -> dict[str, Any]:
        unknown = (
            set(vars(self))
            - set(STATE_FIELDS)
            - set(TRANSIENT_STATE_DEFAULTS)
            - {"logger", "workspace", "on_messages_evicted"}
        )
        if unknown:
            raise ValueError(
                f"Unclassified execution state fields: {sorted(unknown)}"
            )
        data = {
            name: getattr(self, name)
            for name in STATE_FIELDS
            if hasattr(self, name)
        }
        for name in ("tick_signals",):
            if name in data:
                data[name] = sorted(data[name])
        memo = data.get("_tick_decision")
        if memo is not None:
            decision = asdict(memo[2]) if memo[2] is not None else None
            if decision is not None:
                decision["heuristics"] = list(decision["heuristics"])
            data["_tick_decision"] = [memo[0], memo[1], decision, memo[3]]
        data["shared"] = asdict(self.shared)
        data["tool_call_outputs_store"] = (
            self.tool_call_outputs_store.to_dict()
        )
        data["usage"] = self.usage.to_meta()
        data["last_run_usage"] = (
            self.last_run_usage.to_meta() if self.last_run_usage else None
        )
        for key in ("user_intent_log", "current_user_intent"):
            data[key] = serialize_for_meta(getattr(self, key))
        return {"schema_version": 1, "data": deepcopy(data)}

    @classmethod
    def from_execution_snapshot(
        cls, snapshot: dict[str, Any]
    ) -> ExecutionState:
        if snapshot.get("schema_version") != 1:
            raise ValueError("Unsupported execution snapshot version")
        data = deepcopy(snapshot["data"])
        unexpected = set(data) - set(STATE_FIELDS)
        if unexpected:
            raise ValueError(
                f"Unexpected execution fields: {sorted(unexpected)}"
            )
        state = cls.__new__(cls)
        ensure_fields(data)
        for name in ("tick_signals",):
            data[name] = set(data[name])
        memo = data.get("_tick_decision")
        if memo is not None:
            decision = memo[2]
            if decision is not None:
                decision = Decision(
                    **{**decision, "heuristics": tuple(decision["heuristics"])}
                )
            data["_tick_decision"] = (memo[0], memo[1], decision, memo[3])
        data.setdefault("turn_messages_start", None)
        state.__dict__ = data
        state.shared = SharedStateData(**data["shared"])
        state.tool_call_outputs_store = ToolCallOutputsStore.from_dict(
            data["tool_call_outputs_store"]
        )
        state.usage = UsageAccount.from_meta(data["usage"])
        state.last_run_usage = (
            UsageAccount.from_meta(data["last_run_usage"])
            if data.get("last_run_usage")
            else None
        )
        for key in ("user_intent_log", "current_user_intent"):
            setattr(state, key, deserialize_list(data[key]))
        state.logger = None
        state.on_messages_evicted = None
        for name, default in TRANSIENT_STATE_DEFAULTS.items():
            setattr(state, name, default())
        return state
