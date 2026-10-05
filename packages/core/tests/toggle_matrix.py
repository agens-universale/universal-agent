"""Матрица переключателей конфига: один прогон агента на каждое положение.

Зачем. Критерий приёмки переезда — «работает как раньше» при ЛЮБОМ положении
переключателей, а не только при дефолтных. Инвентаризация конфига дала 83
переключателя в четырёх областях; 66 из них проверяемы без живой модели, потому
что ``ScriptedBackend`` детерминирован.

С чем сравнивать. С master сравнить нельзя: там другая раскладка (плоский
``src/`` против ``packages/*/src/``), 132 коммита отставания и нет
``gc_core.aio`` вовсе. Расхождения были бы в основном «за три недели поменяли
фичу», а не «async сломал».

Правильная база — коммит ``aa363554``: первый коммит переезда, где добавлен
ТОЛЬКО этот харнесс, а рантайм идентичен до-async базе ``c96f26ff``. Разница с
нашей веткой — ровно переезд и ничего больше. Харнесс там уже есть, значит
матрицу можно прогнать по обе стороны и сличить дампы побайтово.

Как этим пользоваться::

    # эталон на до-async коде
    git worktree add /tmp/preasync aa363554
    cd /tmp/preasync && uv run python -m toggle_matrix > reference.json

    # текущая ветка
    uv run python -m toggle_matrix > current.json

Сравнение делает ``test_toggle_matrix.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from parity_harness import (
    ScriptedBackend,
    assistant_text,
    assistant_tool_call,
    install,
    judge_accept,
    parity_config,
    run_thread_dump,
    strip_volatile,
    with_path_overrides,
)

#: Сценарий прогона. Тул, финализатор, вердикт судьи — те же три хода, что в
#: приёмке: короче нельзя (не будет тул-колла), длиннее незачем.
TURNS = [
    assistant_tool_call(
        "exec_calculator", {"expression": "2+2"}, call_id="c1"
    ),
    assistant_tool_call(
        "turn_finish",
        {
            "solution_type": "direct_answer",
            "task_report": "готово",
            "handoff": "{}",
        },
        call_id="c2",
    ),
    judge_accept(),
]

#: Хвост для случаев, где включается компакция: сводка и memory-patch — это
#: ДОПОЛНИТЕЛЬНЫЕ обращения к модели, и без запаса сценарий исчерпывается
#: («пришёл вызов №4»), а прогон падает не по той причине, которую проверяем.
#: Хвост «сводок» для summary-случаев. Двенадцать, а не восемь: после правки
#: develop «батч с turn_finish — сначала вся work, закрытие по output» ход
#: тратит на один backend-вызов больше, и на восьми тринадцать summary-случаев
#: падали «сценарий исчерпан» — на САМОМ develop, не только у нас. Запас,
#: а не точное число: `assert_clean` матрица не зовёт, лишние ходы просто не
#: берутся.
SUMMARY_TAIL = [
    assistant_text("сводка предыдущего диалога") for _ in range(40)
]

SYS, TID = "cli", "t-matrix"


class Case:
    """Одно положение переключателей.

    ``overrides`` — пути настроек через точку (``summary.max_message_chars``);
    их накладывает ``parity_harness.with_path_overrides``: правка заменяет
    лист, не затирая соседей по секции.
    """

    def __init__(
        self,
        cid: str,
        area: str,
        overrides: Dict[str, Any],
        note: str = "",
        files: Optional[Dict[str, str]] = None,
    ) -> None:
        self.id = cid
        self.area = area
        self.overrides = overrides
        self.note = note
        #: Файлы, создаваемые в рабочем каталоге ДО прогона, — для ключей,
        #: которые указывают на файл (промпты, скиллы, инструкции). Без них
        #: такой ключ либо игнорируется, либо роняет прогон, и случай проверял
        #: бы не то. Путь в ``overrides`` задаётся через ``{work}``.
        self.files = files or {}


# --- срез B: каждый переключатель поодиночке -------------------------------
#
# Значение отклонения выбрано так, чтобы переключатель ЗАДЕЙСТВОВАЛСЯ на этом
# сценарии, а не просто был записан в конфиг. Где отклонение заведомо ничего не
# меняет на трёх ходах (например потолок в миллион символов), это сказано в
# ``note``: такой случай проверяет, что значение хотя бы не ломает прогон.

SINGLE: List[Case] = [
    # --- лимиты и завершение хода ---
    Case(
        "limits/max_steps_1",
        "limits",
        {"agent.max_steps": 1},
        "лимит меньше числа ходов — прогон обязан закрыться лимитом",
    ),
    Case("limits/max_steps_100", "limits", {"agent.max_steps": 100}),
    Case("limits/max_tool_calls_1", "limits", {"agent.max_tool_calls": 1}),
    Case(
        "limits/session_token_limit",
        "limits",
        {"agent.session_token_limit": 10},
    ),
    Case(
        "limits/no_tool_call_max_1",
        "limits",
        {"agent.no_tool_call_max_count": 1},
    ),
    Case(
        "limits/unknown_tool_repeats_1",
        "limits",
        {"agent.max_unknown_tool_repeats": 1},
    ),
    Case("limits/exec_timeout_1", "limits", {"agent.exec_timeout_sec": 1}),
    Case(
        "limits/exec_capture_50",
        "limits",
        {"agent.exec_output_capture_max_chars": 50},
    ),
    Case(
        "limits/subagent_max_steps_1",
        "limits",
        {"agent.subagent_max_steps": 1},
    ),
    Case(
        "limits/subagents_max_count_0",
        "limits",
        {"agent.subagents_max_count": 0},
    ),
    Case(
        "limits/strict_turn_finish_off",
        "limits",
        {"agent.strict_turn_finish_validation": False},
    ),
    Case(
        "limits/strict_turn_finish_on",
        "limits",
        {"agent.strict_turn_finish_validation": True},
    ),
    # --- суммаризация ---
    Case(
        "summary/trigger_off",
        "summary",
        {"summary.chat_completions_summary_trigger_chars": 0},
        "0 выключает компакцию по порогу, но НЕ force-compact и не /compact",
    ),
    Case(
        "summary/trigger_tiny",
        "summary",
        {
            "summary.chat_completions_summary_trigger_chars": 200,
            "summary.chat_completions_summary_turn_end_trigger_chars": 120,
        },
        "порог заведомо перешагивается — компакция обязана сработать",
    ),
    Case(
        "summary/turn_end_off",
        "summary",
        {
            "summary.chat_completions_summary_trigger_chars": 200,
            "summary.chat_completions_summary_turn_end_trigger_chars": 0,
        },
    ),
    Case(
        "summary/batch_chars_tiny",
        "summary",
        {
            "summary.chat_completions_summary_trigger_chars": 200,
            "summary.incremental_batch_chars": 100,
        },
        "многобатчевая сводка: memory-patch пропускается",
    ),
    Case(
        "summary/max_accumulated_tiny",
        "summary",
        {
            "summary.chat_completions_summary_trigger_chars": 200,
            "summary.max_accumulated_summary_chars": 100,
        },
    ),
    Case(
        "summary/max_message_tiny",
        "summary",
        {
            "summary.chat_completions_summary_trigger_chars": 200,
            "summary.max_message_chars": 100,
        },
    ),
    Case(
        "summary/max_dialogue_tiny",
        "summary",
        {
            "summary.chat_completions_summary_trigger_chars": 200,
            "summary.max_dialogue_chars": 500,
        },
    ),
    Case(
        "summary/last_steps_1",
        "summary",
        {
            "summary.chat_completions_summary_trigger_chars": 200,
            "summary.last_dialog_steps_count": 1,
        },
    ),
    Case(
        "summary/last_step_msg_chars",
        "summary",
        {
            "summary.chat_completions_summary_trigger_chars": 200,
            "summary.last_dialog_step_message_max_chars": 80,
        },
    ),
    Case(
        "summary/intent_preview_chars",
        "summary",
        {"summary.intent_log_preview_chars": 40},
    ),
    # --- гварды ---
    Case(
        "guards/write_guard_off",
        "guards",
        {"security.write_guard.enabled": False},
    ),
    Case(
        "guards/write_guard_on",
        "guards",
        {"security.write_guard.enabled": True},
    ),
    Case(
        "guards/write_guard_strict",
        "guards",
        {
            "security.write_guard.enabled": True,
            "security.write_guard.mode": "strict",
        },
    ),
    Case(
        "guards/read_guard_off",
        "guards",
        {"security.read_guard.enabled": False},
    ),
    Case(
        "guards/read_guard_on", "guards", {"security.read_guard.enabled": True}
    ),
    Case(
        "guards/read_guard_strict",
        "guards",
        {
            "security.read_guard.enabled": True,
            "security.read_guard.mode": "strict",
        },
    ),
    Case(
        "guards/read_denied_ext",
        "guards",
        {
            "security.read_guard.enabled": True,
            "security.read_guard.denied_extensions": [".key", ".pem"],
        },
    ),
    Case(
        "guards/url_policy_off",
        "guards",
        {"security.url_policy.enabled": False},
    ),
    Case(
        "guards/url_policy_on", "guards", {"security.url_policy.enabled": True}
    ),
    Case(
        "guards/url_schemes_https",
        "guards",
        {
            "security.url_policy.enabled": True,
            "security.url_policy.allowed_schemes": ["https"],
        },
    ),
    Case(
        "guards/url_blacklist",
        "guards",
        {
            "security.url_policy.enabled": True,
            "security.url_policy.url_blacklist": ["example.com"],
        },
    ),
    Case(
        "guards/download_max_bytes",
        "guards",
        {"security.download.max_bytes": 1024},
    ),
    Case(
        "guards/moderation_off",
        "guards",
        {"security.command_moderation.enabled": False},
    ),
    Case(
        "guards/stop_tools_finish",
        "guards",
        {"agent.stop_tools": ["turn_finish"]},
    ),
    Case("guards/stop_tools_empty", "guards", {"agent.stop_tools": []}),
    # --- логирование ---
    Case("logging/save_logs_on", "logging", {"logging.save_logs": True}),
    Case("logging/save_logs_off", "logging", {"logging.save_logs": False}),
    Case(
        "logging/console_trace_on", "logging", {"logging.console_trace": True}
    ),
    Case("logging/log_level_debug", "logging", {"logging.log_level": "DEBUG"}),
    Case("logging/log_level_error", "logging", {"logging.log_level": "ERROR"}),
    Case("logging/format_json", "logging", {"logging.format": "json"}),
    Case(
        "logging/redact_on",
        "logging",
        {"logging.redact_secrets.enabled": True},
    ),
    Case(
        "logging/redact_off",
        "logging",
        {"logging.redact_secrets.enabled": False},
    ),
    Case(
        "logging/redact_extra_keys",
        "logging",
        {
            "logging.redact_secrets.enabled": True,
            "logging.redact_secrets.extra_keys": ["мойключ"],
        },
    ),
    Case(
        "logging/trace_header_on",
        "logging",
        {"agent.trace_header_enabled": True},
    ),
    Case(
        "logging/trace_header_off",
        "logging",
        {"agent.trace_header_enabled": False},
    ),
    Case("logging/trace_id_set", "logging", {"trace_id": "trace-матрица"}),
    Case("logging/pricing_on", "logging", {"pricing.enabled": True}),
    Case("logging/pricing_off", "logging", {"pricing.enabled": False}),
    Case("logging/verbose_on", "logging", {"agent.verbose": True}),
    Case(
        "logging/limit_trace_summary_off",
        "logging",
        {"agent.limit_trace_summary": False},
    ),
]


# --- срез B, продолжение: остальные области схемы -------------------------
#
# Первая редакция матрицы покрывала четыре названные области (суммаризация,
# лимиты, гварды, логирование) и трогала 46 листов схемы из 163. Обход схемы
# показал, что за их пределами осталось много живых ключей — они здесь.

SINGLE += [
    # --- backend: форма запроса и разбор ответа ---
    Case("backend/stream_off", "backend", {"backend.stream": False}),
    Case("backend/stream_on", "backend", {"backend.stream": True}),
    Case("backend/use_retry_on", "backend", {"backend.use_retry": True}),
    Case(
        "backend/send_tool_strict_off",
        "backend",
        {"backend.send_tool_strict": False},
    ),
    Case(
        "backend/openai_strict_schema",
        "backend",
        {"backend.openai_strict_tools_schema": True},
    ),
    Case(
        "backend/max_tokens_param",
        "backend",
        {"backend.use_max_tokens_param": True},
    ),
    Case(
        "backend/max_tokens_small",
        "backend",
        {"backend.chat_completions_max_tokens": 512},
    ),
    Case(
        "backend/min_response_tokens",
        "backend",
        {"backend.chat_completions_min_response_tokens": 64},
    ),
    Case(
        "backend/retry_embedded_off",
        "backend",
        {"backend.retry_embedded_choice_errors": False},
    ),
    Case(
        "backend/completion_kwargs",
        "backend",
        {"backend.chat_completion_kwargs": {"temperature": 0.1}},
    ),
    Case(
        "backend/extra_headers",
        "backend",
        {"backend.chat_completion_headers": {"X-Probe": "matrix"}},
        "заголовки HTTP — latin-1, кириллица в них невозможна физически",
    ),
    # --- llm: лимиты токенов у вспомогательных вызовов ---
    Case(
        "llm/max_tokens_small",
        "backend",
        {"chat_completions.llm.max_tokens": 256},
    ),
    Case(
        "llm/judge_max_tokens_small",
        "backend",
        {"chat_completions.llm.judge_max_tokens": 256},
    ),
    Case(
        "llm/completion_kwargs",
        "backend",
        {"chat_completions.llm.completion_kwargs": {"top_p": 0.9}},
    ),
    # --- агент: состав тулов и поведение хода ---
    Case(
        "agent/finish_gate_iters_1",
        "limits",
        {"agent.finish_gate_max_iterations": 1},
    ),
    Case(
        "agent/subagent_finish_gate_1",
        "limits",
        {"agent.subagent_finish_gate_max_iterations": 1},
    ),
    Case(
        "agent/tool_output_inline_small",
        "limits",
        {"agent.tool_output_inline_max_chars": 200},
    ),
    Case(
        "agent/limit_project_metadata",
        "limits",
        {"agent.limit_project_metadata": True},
    ),
    Case(
        "agent/local_subagents_on",
        "limits",
        {"agent.use_local_subagents": True},
    ),
    Case(
        "agent/server_subagents_on",
        "limits",
        {"agent.use_server_subagents": True},
    ),
    Case("agent/disable_skills", "limits", {"agent.disable_skills": ["*"]}),
    Case(
        "agent/server_tool_prefix",
        "limits",
        {"agent.server_tool_prefix": "проба"},
    ),
    Case("agent/system_prompt_id", "limits", {"agent.system_prompt_id": "v1"}),
    Case(
        "agent/x_cl_id",
        "limits",
        {"agent.x_cl_id": "probe-42"},
        "уходит в HTTP-заголовок, поэтому только ASCII",
    ),
    Case(
        "agent/inject_tool_fields",
        "limits",
        {
            "agent.inject_tool_fields": {
                "exec_calculator": {"reason": "почему"}
            }
        },
    ),
    # --- fs_tools: потолки чтения и записи ---
    Case("fs/max_read_small", "guards", {"fs_tools.max_read_bytes": 1024}),
    Case("fs/max_mutate_small", "guards", {"fs_tools.max_mutate_bytes": 1024}),
    # --- intent_scope: сжатие следов тулов ---
    Case(
        "intent/max_summary_small",
        "logging",
        {"intent_scope.max_summary_chars": 50},
    ),
    Case(
        "intent/max_tool_output_small",
        "logging",
        {"intent_scope.max_tool_output_chars": 100},
    ),
    Case(
        "intent/max_raw_record_small",
        "logging",
        {"intent_scope.max_raw_record_chars": 100},
    ),
    Case(
        "intent/max_final_answer_small",
        "logging",
        {"intent_scope.max_final_answer_raw_chars": 100},
    ),
    Case(
        "intent/read_page_limit_1",
        "logging",
        {"intent_scope.read_page_record_limit": 1},
    ),
    Case(
        "intent/fallback_raw_off",
        "logging",
        {"intent_scope.fallback_raw_on_llm_error": False},
    ),
    Case("intent/tools_empty", "logging", {"intent_scope.tools": {}}),
    # --- логирование: оставшиеся ключи ---
    Case(
        "logging/full_log_backend_on",
        "logging",
        {"logging.full_log_backend": True},
    ),
    Case("logging/format_json_flat", "logging", {"logging.format": "json"}),
    Case("logging/propagate_on", "logging", {"logging_propagate": True}),
    Case(
        "logging/redact_extra_patterns",
        "logging",
        {
            "logging.redact_secrets.enabled": True,
            "logging.redact_secrets.extra_patterns": ["секрет-[0-9]+"],
        },
    ),
    Case(
        "logging/trace_id_header_name",
        "logging",
        {
            "agent.trace_header_enabled": True,
            "agent.trace_id_header_name": "X-Мой-Трейс",
        },
    ),
    # --- pricing: учёт стоимости ---
    Case(
        "pricing/currency",
        "logging",
        {"pricing.enabled": True, "pricing.currency": "$"},
    ),
    Case(
        "pricing/per_tokens",
        "logging",
        {"pricing.enabled": True, "pricing.per_tokens": 1000},
    ),
    Case(
        "pricing/rates",
        "logging",
        {
            "pricing.enabled": True,
            "pricing.input": 1.5,
            "pricing.output": 3.0,
            "pricing.cache_read": 0.5,
        },
    ),
    Case(
        "pricing/per_model",
        "logging",
        {
            "pricing.enabled": True,
            "pricing.models": {"parity-model": {"input": 2.0, "output": 4.0}},
        },
    ),
    # --- project_memory ---
    Case(
        "memory/check_interval_1",
        "logging",
        {"project_memory.check_interval_messages": 1},
    ),
    Case(
        "memory/max_chars_small", "logging", {"project_memory.max_chars": 100}
    ),
    Case(
        "memory/max_append_small",
        "logging",
        {"project_memory.max_append_chars": 50},
    ),
    # --- working_memory: бюджеты регистров ---
    Case("wm/goal_small", "summary", {"working_memory.max_chars_goal": 100}),
    Case(
        "wm/constraints_small",
        "summary",
        {"working_memory.max_chars_constraints": 100},
    ),
    Case("wm/state_small", "summary", {"working_memory.max_chars_state": 100}),
    Case(
        "wm/knowledge_small",
        "summary",
        {"working_memory.max_chars_knowledge": 100},
    ),
    Case(
        "wm/total_budget_small",
        "summary",
        {"working_memory.total_budget_chars": 400},
    ),
    # --- гварды: списки путей и примеров ---
    Case(
        "guards/write_extra_paths",
        "guards",
        {
            "security.write_guard.enabled": True,
            "security.write_guard.extra_paths": ["/нет/такого"],
        },
    ),
    Case(
        "guards/read_allowed_roots",
        "guards",
        {
            "security.read_guard.enabled": True,
            "security.read_guard.allowed_read_roots": ["/tmp"],
        },
    ),
    Case(
        "guards/read_allowed_paths",
        "guards",
        {
            "security.read_guard.enabled": True,
            "security.read_guard.allowed_read_paths": ["/tmp/да"],
        },
    ),
    Case(
        "guards/read_denied_paths",
        "guards",
        {
            "security.read_guard.enabled": True,
            "security.read_guard.denied_read_paths": ["/tmp/нет"],
        },
    ),
    Case(
        "guards/read_denied_patterns",
        "guards",
        {
            "security.read_guard.enabled": True,
            "security.read_guard.denied_file_patterns": ["*.секрет"],
        },
    ),
    Case(
        "guards/moderation_examples",
        "guards",
        {
            "security.command_moderation.allowed_command_examples": ["ls"],
            "security.command_moderation.denied_command_examples": [
                "rm -rf /"
            ],
        },
    ),
    Case(
        "guards/download_dir",
        "guards",
        {"security.download.download_dir": "загрузки"},
    ),
    Case(
        "guards/protected_extra",
        "guards",
        {"security.protected_paths.extra": ["/нет/такого/пути"]},
    ),
    # --- прочее ---
    Case(
        "misc/strip_tags", "logging", {"strip_tags_projection.tags": ["think"]}
    ),
    Case("misc/tool_usage_notice", "limits", {"tool_usage_notice": []}),
    Case(
        "misc/mcp_discovery_timeout",
        "limits",
        {"mcp.meta.discovery_timeout_sec": 5},
    ),
    Case("misc/update_check_off", "misc", {"update.check": False}),
    Case(
        "misc/update_extra_args", "misc", {"update.extra_args": ["--dry-run"]}
    ),
]

#: Случаи, чьё положение схема конфига НЕ выражает: значение отвергает модель
#: (и на develop — та же схема), а ключ без поля в схеме не существует. Эталон
#: снят правкой плоского словаря в обход схемы, и потребитель молча игнорировал
#: такое значение — сверять тут нечего. Теперь настройки валидируются строго, и
#: наблюдаемое поведение — громкий отказ сборки; его и проверяет
#: ``test_unrepresentable_case_is_rejected_by_schema``.
UNREPRESENTABLE = {
    "limits/subagents_max_count_0": "agent.subagents_max_count >= 1",
    "guards/write_guard_strict": "security.write_guard.mode: allow|ask|deny",
    "guards/read_guard_strict": "security.read_guard.mode: allow|ask|deny",
    "pair/read_guard_x_skills": "security.read_guard.mode: allow|ask|deny",
    "logging/format_json": "logging.format: text|jsonl",
    "logging/format_json_flat": "logging.format: text|jsonl",
    "logging/propagate_on": "плоского ключа logging_propagate нет (в YAML — logging.propagate)",
    "llm/max_tokens_small": "секции chat_completions.llm в схеме нет",
    "llm/judge_max_tokens_small": "секции chat_completions.llm в схеме нет",
    "llm/completion_kwargs": "секции chat_completions.llm в схеме нет",
    "agent/inject_tool_fields": "agent.inject_tool_fields: {инструмент: имя поля}",
}

#: Ключи схемы, НЕ включённые в матрицу, и почему. Ведётся явно: «не покрыт»
#: и «покрывать нечем» — разные вещи, и отличать их должен читатель, а не
#: тот, кто однажды пересчитает покрытие и решит, что про них забыли.
NOT_IN_MATRIX = {
    # Локально допустимо только step (как develop); turn — только в durable-
    # исполнителе и отклоняется локальным прогоном. Покрыто
    # test_thread_store_persist_turn.py и native test_native_workflow_live.py.
    "thread_store.persist": "local допускает только step; turn проверяется в test_thread_store_persist_turn и native live",
    "agent.model_steering": "текст подготовленных инструкций; текущий reader и local/port parity покрыты test_run_prompt_preparation",
    # Токенный порог автосжатия. Эталон матрицы снят на develop и случаев
    # с этим рычагом не содержит: новый случай нельзя сверить, не пересняв
    # эталон на этой же ветке. Паритет async/sync того же координатора уже
    # смотрят символьные случаи summary/trigger_*. Сам рычаг покрыт
    # test_summary_token_trigger.py и test_context_meter_threshold.py.
    "summary.chat_completions_summary_trigger_tokens": "новый рычаг; эталон develop его не содержит, покрытие в test_summary_token_trigger",
    "summary.chat_completions_summary_turn_end_trigger_tokens": "новый рычаг; эталон develop его не содержит, покрытие в test_summary_token_trigger",
    # Порог напоминания чек-листа. Числился непрочитанным и потому не сверялся
    # с матрицей; эталон develop случаев с ним не содержит. Сам рычаг покрыт
    # test_todo_config.py и test_todo_reminder.py.
    "todo.reminder_steps": "эталон develop его не содержит, покрытие в test_todo_config и test_todo_reminder",
    # --- приехало с develop при вливании 09.09.2026 (PR #97, #99, #100) -------
    #
    # Оба ключа меняют ФОРМУ вывода, а не путь исполнения: preview-края режут
    # уже захваченный поток, strip_think выкидывает теги из проекции. Через
    # место, где async и sync расходятся, они не проходят — случай в матрице
    # сверял бы фичу develop с самой собой. Её покрытие живёт в наборах
    # develop: `test_exec_output_preview.py`, `test_strip_tags_projection*`.
    "agent.exec_output_preview_edge_chars": "форма вывода, не путь исполнения",
    "strip_tags_projection.strip_think": "форма проекции, не путь исполнения",
    # --- приехало с develop при вливании 17.09.2026 (PR #101) -----------------
    #
    # Лимит ответа для генерации роли спецагента: меняет ЧИСЛО в payload
    # aux-вызова, а не путь исполнения. Через место, где async и sync
    # расходятся, не проходит — случай в матрице сверял бы фичу develop с самой
    # собой. Покрытие самой фичи живёт в `test_special_agent.py`.
    "chat_completions.auxiliary_llm.subagent_role_max_tokens": "лимит payload aux-вызова, не путь исполнения",
    # --- приехало с develop при вливании пакетов 08.09.2026 -------------------
    #
    # Матрица сверяет АСИНХРОННОСТЬ: один и тот же сценарий, прогнанный
    # синхронно и асинхронно, обязан дать один и тот же дамп. Ключ попадает
    # сюда, если переключение НЕ проходит через то место, где async и sync
    # расходятся, — тогда случай в матрице проверял бы чужую фичу, а не паритет.
    # Это не «покрыть потом»: покрытие самих фич develop живёт в его наборах.
    # Реестр моделей и профили (PR #80). Переключают ВЫБОР модели, а харнесс
    # маршрутизирует сценарий ровно по `agent.model` — подмена увела бы вызов на
    # несуществующего двойника, и упал бы сам харнесс, а не проверка.
    "assign.auto_judge": "выбор модели: харнесс маршрутизирует по agent.model",
    "assign.command_judge": "выбор модели: харнесс маршрутизирует по agent.model",
    "assign.finish_gate": "выбор модели: харнесс маршрутизирует по agent.model",
    "assign.intent_scope": "выбор модели: харнесс маршрутизирует по agent.model",
    "assign.memory_compaction": "выбор модели: харнесс маршрутизирует по agent.model",
    "assign.subagents": "выбор модели: харнесс маршрутизирует по agent.model",
    "assign.summary": "выбор модели: харнесс маршрутизирует по agent.model",
    "assign.text_answer_judge": "выбор модели: харнесс маршрутизирует по agent.model",
    "assign.vision": "выбор модели: харнесс маршрутизирует по agent.model",
    "level": "выбор модели: харнесс маршрутизирует по agent.model",
    "profile": "выбор модели: харнесс маршрутизирует по agent.model",
    "slots.default": "выбор модели: харнесс маршрутизирует по agent.model",
    "slots.light": "выбор модели: харнесс маршрутизирует по agent.model",
    "slots.max": "выбор модели: харнесс маршрутизирует по agent.model",
    # Разметка prompt cache (PR #96). Врезается в сборку payload и только для
    # claude-моделей — сценарий матрицы гоняет скриптованный бэкенд с моделью
    # `agent.model`, разметка на нём не включается вовсе, и дамп от переключения
    # не меняется. Покрытие самой фичи — `test_prompt_cache.py`.
    "backend.claude_prompt_cache": "разметка payload только для claude-моделей; модель сценария не claude",
    # Заявленная слепота модели (спека изображений). Меняет только судьбу
    # вложений-картинок, а в сценарии матрицы их нет — дамп от переключения не
    # меняется. Покрытие — `vision/test_vision_audience.py`.
    "backend.vision": "действует только на картинки во вложениях; в сценарии матрицы их нет",
    # Тулы нового FS-поиска (PR #86). Сценарий матрицы их не зовёт: у неё три
    # хода с `calculator` и финализатором. Ключи меняют пороги и фильтры внутри
    # самих тулов, до цикла агента не доходят.
    "fs_tools.find_files_by_name.default_max_matches": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_files_by_name.hard_max_matches": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_files_by_name.max_visited": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_in_files.content_workers_threshold": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_in_files.default_limit": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_in_files.exclude_dir_names": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_in_files.exclude_dot_dirs": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_in_files.max_candidates_affix_or": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_in_files.max_candidates_complex": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_in_files.max_candidates_like": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_in_files.max_candidates_or_simple": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_in_files.max_exclude_dirs": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_in_files.max_file_bytes": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_in_files.max_open_files_affix_or": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_in_files.max_total_matches": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_in_files.max_unfiltered_host_reads": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    "fs_tools.find_in_files.top_level_dirs_cap": "порог внутри тула поиска; сценарий матрицы его не зовёт",
    # Изображения во вводе: сценарий матрицы картинок не шлёт, а пороги живости и
    # потолок сообщения меняют состав запроса, не путь исполнения. Покрытие —
    # наборы `attachments/` и `message_policy/` ядра, TUI и CLI.
    "images.max_per_message": "сценарий матрицы картинок не шлёт",
    "images.live_user_images": "сценарий матрицы картинок не шлёт",
    "images.live_tool_images": "сценарий матрицы картинок не шлёт",
    # Голос — дистрибутив gc-tui, ядро его не знает вовсе.
    "voice_endpoint_presets": "голосовой ввод живёт в gc-tui, ядро его не касается",
    "voice.language": "голосовой ввод живёт в gc-tui, ядро его не касается",
    "voice.max_seconds": "голосовой ввод живёт в gc-tui, ядро его не касается",
    "voice.model": "голосовой ввод живёт в gc-tui, ядро его не касается",
    "voice.provider": "голосовой ввод живёт в gc-tui, ядро его не касается",
    "voice.timeout_seconds": "голосовой ввод живёт в gc-tui, ядро его не касается",
    "voice.token": "голосовой ввод живёт в gc-tui, ядро его не касается",
    "voice.url": "голосовой ввод живёт в gc-tui, ядро его не касается",
    "voice.verify_ssl": "голосовой ввод живёт в gc-tui, ядро его не касается",
    # Одиночные потолки, приехавшие с develop: меняют размер запроса, а не путь
    # исполнения. Асинхронность на них не завязана.
    "agent.walk_output_max_chars": "потолок обхода бэкенда; сценарий не обходит ФС",
    "agent.harbor_exec_init": "только Harbor: префикс канала модели в песочнице; сценарии матрицы локальные, покрыт test_harbor_backend",
    "chat_completions.llm.subagent_role_max_tokens": "потолок запроса роли субагента",
    "project_memory.compaction_max_tokens": "потолок компакции проектной памяти",
    # Задаёт сам харнесс: подмена сломала бы маршрутизацию ScriptedBackend.
    "agent.model": "выставляет parity_config; по нему маршрутизируется сценарий",
    "backend.completions_url": "то же — адрес, который перехватывает MockTransport",
    # ScriptedBackend отдаёт ответы формата chat_completions; в responses-режиме
    # ядро ждёт другую форму и падает на разборе. Это ограничение ХАРНЕССА, а не
    # пробел покрытия: сам режим проверяют test_responses_* и парити-набор.
    "backend.api_mode": "харнесс умеет только chat_completions (ScriptedBackend)",
    # Сеть и TLS: MockTransport до них не доходит.
    "backend.use_ssl": "TLS не участвует: транспорт подменён",
    "backend.cert_path": "то же",
    "backend.key_path": "то же",
    "backend.proxy": "то же",
    "backend.request_timeout_sec": "нужен живой медленный бэкенд — L4",
    "mcp.meta.request_timeout_sec": "нужен живой MCP-сервер — L4",
    "mcp.servers": "нужны живые серверы — L3/L4",
    "agent_auth": "внешняя авторизация, нужен живой провайдер",
    "endpoint_presets": "выбор эндпойнта, нужен живой провайдер",
    # Требуют файлов, которых на прогоне нет; проверяются своими тестами.
    "paths.chat_completions_system_prompt_path": "выставляет parity_config",
    "paths.threads_data_path": "выставляет parity_config — туда пишется тред",
    "paths.logs_dump_dir": "выставляет parity_config",
    # Только TUI: матрица гоняет CLI-прогон.
    "tui.stream": "только TUI",
    "tui.text_answer_mode": "только TUI",
    "acp.text_answer_mode": "только ACP",
    "acp.finish_gate_mode": "только ACP",
    "acp.todo_reminder_steps": "только ACP",
    "tui.finish_gate_mode": "только TUI",
    "chat_api.host": "только процесс gc-chat",
    "chat_api.port": "только процесс gc-chat",
    "chat_api.auth_token": "только процесс gc-chat",
    "chat_api.cors_origins": "только процесс gc-chat",
    "chat_api.launch_root": "только процесс gc-chat",
    "chat_api.text_answer_mode": "только процесс gc-chat",
    "chat_api.finish_gate_mode": "только процесс gc-chat",
    "chat_api.todo_reminder_steps": "только процесс gc-chat",
    "todo.reminder_steps": (
        "напоминание чеклиста; матрица гоняет CLI и этот лист не "
        "переключает. Канал Responses пишет его в runtime"
    ),
    "tui.agentsmd_override": "только TUI",
    "tui.bang_shell.timeout_sec": "только TUI",
    "tui.bang_shell.max_output_bytes": "только TUI",
    "tui.bang_shell.max_output_lines": "только TUI",
    "tui.file_view.max_file_bytes": "только TUI",
    "tui.file_view.page_lines": "только TUI",
    # --- секция agent_edits: правки агента вне проекта (журнал, снимки, откат) ---
    #
    # Ветка переименовала `cache_max_orig` в `snapshots_max_files`, убрала
    # `preview_context_lines` и `full_file_max_lines` (см. `config_modules.compat`)
    # и завела шесть новых ключей. Критерий тот же, что у блоков выше: ключ
    # попадает сюда, если его переключение не меняет НИ ОДНОГО сравниваемого
    # поля — ответа, `data_*.jsonl`, отобранных полей meta, числа обращений к
    # бэкенду, текста отказа.
    #
    # Журнал правок — ОТДЕЛЬНЫЙ канал записи: записи уходят в
    # `edits_<system>_<tid>.jsonl`, базы отката — в `<launch_root>/.gc/agent_edits`,
    # а сравниваемый дамп читает только `data_*.jsonl`. Вдобавок сценарий
    # матрицы (калькулятор, финализатор, вердикт судьи) не правит ни одного
    # файла: писатели канала — fs-тулы и шелл-делта — на нём не просыпаются.
    "agent_edits.snapshots_max_files": "потолок вытеснения снимков в .gc/agent_edits; сценарий не правит файлы, покрыт agent_edits/test_snapshots",
    "agent_edits.hunk_context_lines": "контекст хэнков в записи журнала; там же, покрыт agent_edits/test_deltas",
    "agent_edits.max_diff_source_bytes": "порог, выше которого дифф не считается и снимок не снимается; там же",
    "agent_edits.max_diff_chars": "потолок текста хэнков в записи журнала; там же (раньше числился «только TUI» — ветка увела читателей в ядро)",
    # `shell_tracking` — единственный из секции, который ДЕЙСТВИТЕЛЬНО стоит на
    # пути, переехавшем на корутины: он гасит `ShellDelta.begin` внутри
    # `TrackingFsBackend._atrack_shell_delta`, то есть ровно вокруг запуска
    # команды. Исключается не поэтому, а по факту сверки: сценарий матрицы
    # команд не запускает вовсе — `exec_calculator` считает выражение в
    # процессе и до бэкенда не доходит, — и обёртка ни в одном случае в ветку с
    # делтой не входит. Даже войди она, писать ей некуда: канал кладёт записи в
    # `edits_*.jsonl` и снимки, а сверяются ответ, `data_*.jsonl`, meta, число
    # обращений к бэкенду и отказ. Асинхронность самой обёртки покрыта
    # `agent_edits/test_tracking_async_exec.py` и `test_tracking_probe_off_loop.py`,
    # сам переключатель — `test_shell_delta.py::test_shell_tracking_false_writes_nothing`.
    "agent_edits.shell_tracking": "гейт шелл-делты; сценарий матрицы не запускает ни одной команды",
    # Показ, а не запись: эти три читает только `gc_tui.screens.main`. Матрица
    # гоняет CLI-прогон, до ленты TUI он не доходит. Покрытие — наборы TUI
    # `test_trace_diff_block.py`, `test_trace_diff_replay.py`,
    # `test_turn_summary_segment.py`.
    "agent_edits.trace_inline_max_lines": "только TUI: свёртка инлайн-диффа в ленте",
    "agent_edits.replay_expanded_turns": "только TUI: окно развёрнутых ходов при открытии треда",
    "agent_edits.turn_summary_in_status": "только TUI: сводка правок в строке-итоге хода",
    # Запись журнала правок по режиму запуска: пишет отдельный канал (журнал и
    # снимки), сравниваемый дамп его не читает, а сценарий матрицы файлов не
    # правит. Покрыто agent_edits/test_ctx и test_run_context_wraps_backend.
    "agent_edits.record.tui": "запись журнала правок в TUI; покрыто agent_edits/test_ctx",
    "agent_edits.record.cli": "запись журнала правок в gc-cli; там же",
    "agent_edits.record.server": "запись журнала правок на сервере; там же",
    # --- единственный случай матрицы задавал значение, невыразимое схемой ----
    #
    # См. UNREPRESENTABLE: эталон develop снят правкой плоского словаря в обход
    # схемы, и для этих листов валидного случая в нём нет — сверять нечего, а
    # переснять эталон с этой ветки нельзя. Поведение покрыто наборами ниже.
    "security.write_guard.mode": "в эталоне только невалидный strict; покрытие — security/test_guards_dual_mode, test_native_tool_effects",
    "security.read_guard.mode": "в эталоне только невалидный strict; покрытие — security/test_guard_defaults, security/test_native_guard_probe",
    "security.command_moderation.mode": "в эталоне только невалидный strict (лист mode); покрытие — test_command_moderation_config",
    "security.url_policy.mode": "в эталоне только невалидный strict (лист mode); покрытие — security/test_guards_dual_mode",
    "logging.format": "в эталоне только невалидный json; покрытие — cli/test_structured_output, test_logging_emission_dispatch3",
    "logging.propagate": "в эталоне только несуществующий плоский logging_propagate (флаг не доходил); покрытие — test_log_handlers_injectable",
    "agent.inject_tool_fields": "в эталоне только невалидная форма значения; покрытие — test_inject_tool_fields, test_schema_optional",
    "chat_completions.auxiliary_llm.max_tokens": "в эталоне только несуществующая секция chat_completions.llm; покрытие — test_summary_llm_request",
    "chat_completions.auxiliary_llm.judge_max_tokens": "в эталоне только несуществующая секция chat_completions.llm; покрытие — test_finish_gate_handoff_form",
    "chat_completions.auxiliary_llm.completion_kwargs": "в эталоне только несуществующая секция chat_completions.llm; покрытие — test_summary_llm_request",
    # --- читатель был скрыт в помощниках config_modules (KNOWN_UNREAD по ошибке)
    "mcp.meta.recommended_mcp_manifest": "путь манифеста для gc-cli -a/install, не ход агента; покрытие — mcp/test_manifest_versions",
    "project_memory.enable_project_memory_notice": "эталон develop случая не содержит; покрытие — test_project_memory_notice_policy",
    "todo.reminder_steps": "эталон develop случая не содержит; покрытие — test_todo_reminder, test_todo_config",
    "tui.stream_display": "только отображение TUI, не ход агента; покрытие — tui/test_tui_config",
}


# --- срез B: скиллы, промпты и инструкции ---------------------------------
#
# У них есть свои наборы (test_skill_*, test_*prompt*), но там проверяют сам
# механизм. Здесь другое: прогон ЦЕЛИКОМ не изменился от того, что скилл
# подложен, а промпт заменён. Ключ, указывающий на файл, без файла либо
# игнорируется, либо роняет прогон — поэтому файлы создаются вместе со случаем.

SINGLE += [
    Case(
        "skills/catalog_present",
        "skills",
        {"paths.skills_data_path": "{work}/skills"},
        "каталог скиллов подложен — состав тулов и промпт обязаны совпасть",
        files={
            "skills/проба/SKILL.md": "---\nname: проба\ndescription: пробный скилл матрицы\n---\n\nТело скилла.\n"
        },
    ),
    Case(
        "skills/catalog_empty",
        "skills",
        {"paths.skills_data_path": "{work}/skills"},
        "каталог есть, но пуст",
        files={"skills/.keep": ""},
    ),
    Case(
        "skills/disabled_all",
        "skills",
        {
            "paths.skills_data_path": "{work}/skills",
            "agent.disable_skills": ["*"],
        },
        files={
            "skills/проба/SKILL.md": "---\nname: проба\ndescription: пробный скилл матрицы\n---\n\nТело.\n"
        },
    ),
    Case(
        "skills/disabled_by_name",
        "skills",
        {
            "paths.skills_data_path": "{work}/skills",
            "agent.disable_skills": ["проба"],
        },
        files={
            "skills/проба/SKILL.md": "---\nname: проба\ndescription: пробный скилл матрицы\n---\n\nТело.\n"
        },
    ),
    Case(
        "prompts/system_replaced",
        "prompts",
        {"paths.chat_completions_system_prompt_path": "{work}/prompts/sys.md"},
        "системный промпт заменён — он уезжает в meta и в каждый запрос",
        files={"prompts/sys.md": "СИСТЕМНЫЙ ПРОМПТ МАТРИЦЫ"},
    ),
    Case(
        "prompts/special_instructions",
        "prompts",
        {
            "paths.chat_completions_special_instructions_path": "{work}/prompts/special.md"
        },
        files={"prompts/special.md": "ОСОБЫЕ УКАЗАНИЯ МАТРИЦЫ"},
    ),
    Case(
        "prompts/intent_guard",
        "prompts",
        {
            "paths.chat_completions_intent_guard_prompt_path": "{work}/prompts/guard.md"
        },
        files={"prompts/guard.md": "ПРОМПТ СУДЬИ МАТРИЦЫ"},
    ),
    Case(
        "prompts/subagent_system",
        "prompts",
        {
            "paths.chat_completions_custom_subagent_system_prompt_path": "{work}/prompts/sub.md"
        },
        files={"prompts/sub.md": "ПРОМПТ СУБАГЕНТА МАТРИЦЫ"},
    ),
    Case(
        "prompts/summary_prompt",
        "prompts",
        {
            "summary.chat_completions_summary_trigger_chars": 200,
            "summary.chat_completions_summary_prompt_path": "{work}/prompts/summary.md",
        },
        "промпт сводки заменён и компакция реально идёт",
        files={"prompts/summary.md": "ПРОМПТ СВОДКИ МАТРИЦЫ"},
    ),
    Case(
        "prompts/memory_patch_prompt",
        "prompts",
        {
            "summary.chat_completions_summary_trigger_chars": 200,
            "summary.chat_completions_memory_patch_replace_prompt_path": "{work}/prompts/mem.md",
        },
        files={"prompts/mem.md": "ПРОМПТ ПАМЯТИ МАТРИЦЫ"},
    ),
    Case(
        "prompts/intent_scope_tool",
        "prompts",
        {
            "intent_scope.intent_scope_tool_summary_prompt_path": "{work}/prompts/is_tool.md"
        },
        files={"prompts/is_tool.md": "ПРОМПТ СЖАТИЯ ТУЛА"},
    ),
    Case(
        "prompts/intent_scope_final",
        "prompts",
        {
            "intent_scope.intent_scope_final_answer_summary_prompt_path": "{work}/prompts/is_final.md"
        },
        files={"prompts/is_final.md": "ПРОМПТ СЖАТИЯ ОТВЕТА"},
    ),
    Case(
        "prompts/agent_md",
        "prompts",
        {"paths.agent_md_path": "{work}/AGENTS.md"},
        "проектные инструкции подложены",
        files={"AGENTS.md": "# Проектные указания\n\nПравило матрицы.\n"},
    ),
    Case(
        "prompts/subagents_dir",
        "prompts",
        {
            "agent.subagents_dir": "{work}/subagents",
            "agent.use_local_subagents": True,
        },
        "каталог субагентов подложен",
        files={
            "subagents/помощник.md": "---\nname: subagent_помощник\ndescription: пробный субагент\n---\n\nТело.\n"
        },
    ),
]


# --- срез C: пары, у которых есть ОБЩЕЕ место в коде -----------------------
#
# Не произвольные сочетания: каждая пара выбрана потому, что оба переключателя
# читаются одним и тем же участком, и разъехаться они могут только вместе.

PAIRS: List[Case] = [
    Case(
        "pair/trigger_x_turn_end",
        "summary",
        {
            "summary.chat_completions_summary_trigger_chars": 200,
            "summary.chat_completions_summary_turn_end_trigger_chars": 100,
        },
        "run_summary_coordinator: pre_run/pre_backend против turn_end",
    ),
    Case(
        "pair/trigger_x_max_steps",
        "summary",
        {
            "summary.chat_completions_summary_trigger_chars": 200,
            "agent.max_steps": 2,
        },
        "компакция внутри лимита шагов",
    ),
    Case(
        "pair/batch_x_accumulated",
        "summary",
        {
            "summary.chat_completions_summary_trigger_chars": 200,
            "summary.incremental_batch_chars": 100,
            "summary.max_accumulated_summary_chars": 80,
        },
        "сжатие сводки внутри многобатча",
    ),
    Case(
        "pair/msg_x_last_step_msg",
        "summary",
        {
            "summary.chat_completions_summary_trigger_chars": 200,
            "summary.max_message_chars": 100,
            "summary.last_dialog_step_message_max_chars": 500,
        },
        "min() на thread_summarizer: какой потолок победил",
    ),
    Case(
        "pair/save_logs_x_redact",
        "logging",
        {"logging.save_logs": True, "logging.redact_secrets.enabled": True},
    ),
    Case(
        "pair/write_guard_x_download",
        "guards",
        {
            "security.write_guard.enabled": True,
            "security.download.max_bytes": 2048,
        },
    ),
    Case(
        "pair/read_guard_x_skills",
        "guards",
        {
            "security.read_guard.enabled": True,
            "security.read_guard.mode": "strict",
        },
    ),
    Case(
        "pair/stop_tools_x_subagents",
        "guards",
        {"agent.stop_tools": ["turn_finish"], "agent.subagents_max_count": 1},
    ),
    Case(
        "pair/trace_x_pricing",
        "logging",
        {"agent.trace_header_enabled": True, "pricing.enabled": True},
    ),
    Case(
        "pair/tool_calls_x_no_tool_call",
        "limits",
        {"agent.max_tool_calls": 2, "agent.no_tool_call_max_count": 1},
    ),
    Case(
        "pair/exec_timeout_x_capture",
        "limits",
        {
            "agent.exec_timeout_sec": 2,
            "agent.exec_output_capture_max_chars": 40,
        },
    ),
    Case(
        "pair/console_trace_x_level",
        "logging",
        {"logging.console_trace": True, "logging.log_level": "ERROR"},
    ),
    Case(
        "pair/max_steps_x_strict_finish",
        "limits",
        {"agent.max_steps": 2, "agent.strict_turn_finish_validation": True},
    ),
    Case(
        "pair/pricing_x_token_limit",
        "limits",
        {"pricing.enabled": True, "agent.session_token_limit": 50},
    ),
    Case(
        "pair/url_x_download_dir",
        "guards",
        {
            "security.url_policy.enabled": True,
            "security.download.max_bytes": 512,
        },
    ),
]

BASELINE = Case("baseline/default", "baseline", {}, "дефолтный конфиг")

ALL_CASES: List[Case] = [BASELINE, *SINGLE, *PAIRS]


async def run_case(
    case: Case, tmp_root: Path, monkeypatch: Any
) -> Dict[str, Any]:
    """Один прогон. Возвращает наблюдаемое: ответ, дамп треда, meta, ошибку."""
    # Гарнис обязан идти на ОБЕИХ ветках: эталон снимается прогоном этого же
    # файла в worktree на develop, где сборка контекста синхронная. Ветку
    # выбираем по наличию имени, а не по вере.
    from gc_core import run_context as _rc
    from gc_core.agent_executor import AgentExecutor

    work = tmp_root / case.id.replace("/", "__")
    base = parity_config(work, monkeypatch)

    for rel, content in case.files.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    overrides: Dict[str, Any] = {}
    for path, value in case.overrides.items():
        if isinstance(value, str) and "{work}" in value:
            value = value.format(work=work)
        overrides[path] = value

    backend = install(monkeypatch, ScriptedBackend([*TURNS, *SUMMARY_TAIL]))
    result: Dict[str, Any] = {
        "id": case.id,
        "area": case.area,
        "note": case.note,
    }
    try:
        # Правки — внутри try: невыразимое схемой значение (UNREPRESENTABLE)
        # отказывает на валидации настроек, и отказ — наблюдаемое поведение.
        cfg = with_path_overrides(base, overrides)
        _acreate = getattr(_rc, "acreate_run_context", None)
        if _acreate is not None:
            ctx = await _acreate(cfg)
        else:
            ctx = _rc.create_run_context(cfg)
        executor = AgentExecutor(ctx)
        # СПИСОК сообщений, а не строка: arun принимает messages, и строка
        # разошлась бы на символы — user_intent_log записал бы каждую букву
        # отдельной записью. Так же оборачивает SDK-фасад (AsyncAgent.run).
        _arun = getattr(executor, "arun", None)
        if _arun is not None:
            answer = await _arun(
                ["сделай что-нибудь"], thread_id=TID, system=SYS
            )
        else:
            answer = executor.run(
                ["сделай что-нибудь"], thread_id=TID, system=SYS
            )
        result["answer"] = (
            strip_volatile(answer)
            if isinstance(answer, (dict, list))
            else answer
        )
        result["error"] = None
    except Exception as exc:  # фиксируем ЛЮБОЙ отказ
        # Отказ — тоже наблюдаемое поведение и тоже подлежит сверке: важно, что
        # он ОДИНАКОВ по обе стороны, а не что его нет.
        result["answer"] = None
        result["error"] = f"{type(exc).__name__}: {exc}"

    # Каталог тредов правки не трогают: дамп снимается и после отказа валидации.
    result["dump"] = run_thread_dump(base, SYS, TID)
    result["backend_calls"] = len(getattr(backend, "calls", []))

    try:
        from gc_core.thread_store import load_thread_meta, resolve_threads_dir

        meta = load_thread_meta(resolve_threads_dir(base), SYS, TID) or {}
        result["meta"] = strip_volatile(
            {
                k: v
                for k, v in meta.items()
                if k
                in (
                    "messages_count",
                    "backend_api_mode",
                    "runtime",
                    "dialog_steps_offset",
                    "user_turn_seq",
                )
            }
        )
    except Exception:
        result["meta"] = {}
    return result


async def run_all(tmp_root: Path) -> List[Dict[str, Any]]:
    """Прогнать всю матрицу. Каждый случай — со своим ``monkeypatch``."""
    from _pytest.monkeypatch import MonkeyPatch

    out: List[Dict[str, Any]] = []
    for case in ALL_CASES:
        mp = MonkeyPatch()
        try:
            out.append(await run_case(case, tmp_root, mp))
        finally:
            mp.undo()
    return out


def main() -> None:
    """Прогнать матрицу и записать результат в файл из argv[1].

    Именно в файл, а не в stdout: агент печатает туда трассу суммаризации и
    вывод тулов, и JSON оказался бы перемешан с ними.
    """
    import tempfile

    if len(sys.argv) < 2:
        raise SystemExit(
            "использование: python toggle_matrix.py <файл-результата>"
        )
    root = Path(tempfile.mkdtemp(prefix="toggle-matrix-"))
    results = asyncio.run(run_all(root))
    Path(sys.argv[1]).write_text(
        json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    errors = [r for r in results if r.get("error")]
    print(
        f"случаев: {len(results)}, с отказом: {len(errors)}", file=sys.stderr
    )


if __name__ == "__main__":
    main()
