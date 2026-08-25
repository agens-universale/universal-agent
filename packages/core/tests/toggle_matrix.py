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

import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from parity_harness import (  # noqa: E402
    ScriptedBackend,
    assistant_text,
    assistant_tool_call,
    install,
    judge_accept,
    parity_config,
    run_thread_dump,
    strip_volatile,
)

#: Сценарий прогона. Тул, финализатор, вердикт судьи — те же три хода, что в
#: приёмке: короче нельзя (не будет тул-колла), длиннее незачем.
TURNS = [
    assistant_tool_call("exec_calculator", {"expression": "2+2"}, call_id="c1"),
    assistant_tool_call("turn_finish", {
        "solution_type": "direct_answer", "task_report": "готово", "handoff": "{}",
    }, call_id="c2"),
    judge_accept(),
]

#: Хвост для случаев, где включается компакция: сводка и memory-patch — это
#: ДОПОЛНИТЕЛЬНЫЕ обращения к модели, и без запаса сценарий исчерпывается
#: («пришёл вызов №4»), а прогон падает не по той причине, которую проверяем.
SUMMARY_TAIL = [assistant_text("сводка предыдущего диалога") for _ in range(8)]

SYS, TID = "cli", "t-matrix"


class Case:
    """Одно положение переключателей.

    ``overrides`` — точечные пути в ПЛОСКОМ словаре конфига
    (``summary_params.max_message_chars``), потому что вложенные секции там
    остались словарями, а ``cfg.update()`` затёр бы такую секцию целиком.
    """

    def __init__(self, cid: str, area: str, overrides: Dict[str, Any],
                 note: str = "") -> None:
        self.id = cid
        self.area = area
        self.overrides = overrides
        self.note = note


def _set_dotted(cfg: Dict[str, Any], path: str, value: Any) -> None:
    """Проставить значение по точечному пути, не затирая соседей по секции."""
    parts = path.split(".")
    node: Any = cfg
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
        else:
            nxt = dict(nxt)          # копия: секция могла приехать из дефолтов
        node[part] = nxt
        node = nxt
    node[parts[-1]] = value


# --- срез B: каждый переключатель поодиночке -------------------------------
#
# Значение отклонения выбрано так, чтобы переключатель ЗАДЕЙСТВОВАЛСЯ на этом
# сценарии, а не просто был записан в конфиг. Где отклонение заведомо ничего не
# меняет на трёх ходах (например потолок в миллион символов), это сказано в
# ``note``: такой случай проверяет, что значение хотя бы не ломает прогон.

SINGLE: List[Case] = [
    # --- лимиты и завершение хода ---
    Case("limits/max_steps_1", "limits", {"max_steps": 1},
         "лимит меньше числа ходов — прогон обязан закрыться лимитом"),
    Case("limits/max_steps_100", "limits", {"max_steps": 100}),
    Case("limits/max_tool_calls_1", "limits", {"max_tool_calls": 1}),
    Case("limits/session_token_limit", "limits", {"session_token_limit": 10}),
    Case("limits/no_tool_call_max_1", "limits", {"no_tool_call_max_count": 1}),
    Case("limits/unknown_tool_repeats_1", "limits", {"max_unknown_tool_repeats": 1}),
    Case("limits/exec_timeout_1", "limits", {"exec_timeout_sec": 1}),
    Case("limits/exec_capture_50", "limits", {"exec_output_capture_max_chars": 50}),
    Case("limits/subagent_max_steps_1", "limits", {"subagent_max_steps": 1}),
    Case("limits/subagents_max_count_0", "limits", {"subagents_max_count": 0}),
    Case("limits/strict_turn_finish_off", "limits",
         {"strict_turn_finish_validation": False}),
    Case("limits/strict_turn_finish_on", "limits",
         {"strict_turn_finish_validation": True}),

    # --- суммаризация ---
    Case("summary/trigger_off", "summary",
         {"summary_params.chat_completions_summary_trigger_chars": 0},
         "0 выключает компакцию по порогу, но НЕ force-compact и не /compact"),
    Case("summary/trigger_tiny", "summary",
         {"summary_params.chat_completions_summary_trigger_chars": 200,
          "summary_params.chat_completions_summary_turn_end_trigger_chars": 120},
         "порог заведомо перешагивается — компакция обязана сработать"),
    Case("summary/turn_end_off", "summary",
         {"summary_params.chat_completions_summary_trigger_chars": 200,
          "summary_params.chat_completions_summary_turn_end_trigger_chars": 0}),
    Case("summary/batch_chars_tiny", "summary",
         {"summary_params.chat_completions_summary_trigger_chars": 200,
          "summary_params.incremental_batch_chars": 100},
         "многобатчевая сводка: memory-patch пропускается"),
    Case("summary/max_accumulated_tiny", "summary",
         {"summary_params.chat_completions_summary_trigger_chars": 200,
          "summary_params.max_accumulated_summary_chars": 100}),
    Case("summary/max_message_tiny", "summary",
         {"summary_params.chat_completions_summary_trigger_chars": 200,
          "summary_params.max_message_chars": 100}),
    Case("summary/max_dialogue_tiny", "summary",
         {"summary_params.chat_completions_summary_trigger_chars": 200,
          "summary_params.max_dialogue_chars": 500}),
    Case("summary/last_steps_1", "summary",
         {"summary_params.chat_completions_summary_trigger_chars": 200,
          "summary_params.last_dialog_steps_count": 1}),
    Case("summary/last_step_msg_chars", "summary",
         {"summary_params.chat_completions_summary_trigger_chars": 200,
          "summary_params.last_dialog_step_message_max_chars": 80}),
    Case("summary/intent_preview_chars", "summary",
         {"summary_params.intent_log_preview_chars": 40}),

    # --- гварды ---
    Case("guards/write_guard_off", "guards", {"write_guard.enabled": False}),
    Case("guards/write_guard_on", "guards", {"write_guard.enabled": True}),
    Case("guards/write_guard_strict", "guards",
         {"write_guard.enabled": True, "write_guard.mode": "strict"}),
    Case("guards/read_guard_off", "guards", {"read_guard.enabled": False}),
    Case("guards/read_guard_on", "guards", {"read_guard.enabled": True}),
    Case("guards/read_guard_strict", "guards",
         {"read_guard.enabled": True, "read_guard.mode": "strict"}),
    Case("guards/read_denied_ext", "guards",
         {"read_guard.enabled": True, "read_guard.denied_extensions": [".key", ".pem"]}),
    Case("guards/url_policy_off", "guards", {"url_policy.enabled": False}),
    Case("guards/url_policy_on", "guards", {"url_policy.enabled": True}),
    Case("guards/url_schemes_https", "guards",
         {"url_policy.enabled": True, "url_policy.allowed_schemes": ["https"]}),
    Case("guards/url_blacklist", "guards",
         {"url_policy.enabled": True, "url_policy.url_blacklist": ["example.com"]}),
    Case("guards/download_max_bytes", "guards", {"download.max_bytes": 1024}),
    Case("guards/moderation_off", "guards", {"command_moderation.enabled": False}),
    Case("guards/stop_tools_finish", "guards", {"stop_tools": ["turn_finish"]}),
    Case("guards/stop_tools_empty", "guards", {"stop_tools": []}),

    # --- логирование ---
    Case("logging/save_logs_on", "logging", {"save_logs": True}),
    Case("logging/save_logs_off", "logging", {"save_logs": False}),
    Case("logging/console_trace_on", "logging", {"console_trace": True}),
    Case("logging/log_level_debug", "logging", {"log_level": "DEBUG"}),
    Case("logging/log_level_error", "logging", {"log_level": "ERROR"}),
    Case("logging/format_json", "logging", {"logging_format": "json"}),
    Case("logging/redact_on", "logging", {"redact_secrets.enabled": True}),
    Case("logging/redact_off", "logging", {"redact_secrets.enabled": False}),
    Case("logging/redact_extra_keys", "logging",
         {"redact_secrets.enabled": True, "redact_secrets.extra_keys": ["мойключ"]}),
    Case("logging/trace_header_on", "logging", {"trace_header_enabled": True}),
    Case("logging/trace_header_off", "logging", {"trace_header_enabled": False}),
    Case("logging/trace_id_set", "logging", {"trace_id": "trace-матрица"}),
    Case("logging/pricing_on", "logging", {"pricing.enabled": True}),
    Case("logging/pricing_off", "logging", {"pricing.enabled": False}),
    Case("logging/verbose_on", "logging", {"verbose": True}),
    Case("logging/limit_trace_summary_off", "logging", {"limit_trace_summary": False}),
]

# --- срез C: пары, у которых есть ОБЩЕЕ место в коде -----------------------
#
# Не произвольные сочетания: каждая пара выбрана потому, что оба переключателя
# читаются одним и тем же участком, и разъехаться они могут только вместе.

PAIRS: List[Case] = [
    Case("pair/trigger_x_turn_end", "summary",
         {"summary_params.chat_completions_summary_trigger_chars": 200,
          "summary_params.chat_completions_summary_turn_end_trigger_chars": 100},
         "run_summary_coordinator: pre_run/pre_backend против turn_end"),
    Case("pair/trigger_x_max_steps", "summary",
         {"summary_params.chat_completions_summary_trigger_chars": 200,
          "max_steps": 2},
         "компакция внутри лимита шагов"),
    Case("pair/batch_x_accumulated", "summary",
         {"summary_params.chat_completions_summary_trigger_chars": 200,
          "summary_params.incremental_batch_chars": 100,
          "summary_params.max_accumulated_summary_chars": 80},
         "сжатие сводки внутри многобатча"),
    Case("pair/msg_x_last_step_msg", "summary",
         {"summary_params.chat_completions_summary_trigger_chars": 200,
          "summary_params.max_message_chars": 100,
          "summary_params.last_dialog_step_message_max_chars": 500},
         "min() на thread_summarizer: какой потолок победил"),
    Case("pair/save_logs_x_redact", "logging",
         {"save_logs": True, "redact_secrets.enabled": True}),
    Case("pair/write_guard_x_download", "guards",
         {"write_guard.enabled": True, "download.max_bytes": 2048}),
    Case("pair/read_guard_x_skills", "guards",
         {"read_guard.enabled": True, "read_guard.mode": "strict"}),
    Case("pair/stop_tools_x_subagents", "guards",
         {"stop_tools": ["turn_finish"], "subagents_max_count": 1}),
    Case("pair/trace_x_pricing", "logging",
         {"trace_header_enabled": True, "pricing.enabled": True}),
    Case("pair/tool_calls_x_no_tool_call", "limits",
         {"max_tool_calls": 2, "no_tool_call_max_count": 1}),
    Case("pair/exec_timeout_x_capture", "limits",
         {"exec_timeout_sec": 2, "exec_output_capture_max_chars": 40}),
    Case("pair/console_trace_x_level", "logging",
         {"console_trace": True, "log_level": "ERROR"}),
    Case("pair/max_steps_x_strict_finish", "limits",
         {"max_steps": 2, "strict_turn_finish_validation": True}),
    Case("pair/pricing_x_token_limit", "limits",
         {"pricing.enabled": True, "session_token_limit": 50}),
    Case("pair/url_x_download_dir", "guards",
         {"url_policy.enabled": True, "download.max_bytes": 512}),
]

BASELINE = Case("baseline/default", "baseline", {}, "дефолтный конфиг")

ALL_CASES: List[Case] = [BASELINE, *SINGLE, *PAIRS]


def run_case(case: Case, tmp_root: Path, monkeypatch: Any) -> Dict[str, Any]:
    """Один прогон. Возвращает наблюдаемое: ответ, дамп треда, meta, ошибку."""
    from gc_core.agent_executor import AgentExecutor
    from gc_core.run_context import create_run_context

    work = tmp_root / case.id.replace("/", "__")
    cfg = parity_config(work, monkeypatch)
    for path, value in case.overrides.items():
        _set_dotted(cfg, path, value)

    backend = install(monkeypatch, ScriptedBackend([*TURNS, *SUMMARY_TAIL]))
    result: Dict[str, Any] = {"id": case.id, "area": case.area, "note": case.note}
    try:
        ctx = create_run_context(cfg)
        executor = AgentExecutor(ctx)
        # СПИСОК сообщений, а не строка: arun принимает messages, и строка
        # разошлась бы на символы — user_intent_log записал бы каждую букву
        # отдельной записью. Так же оборачивает SDK-фасад (AsyncAgent.run).
        answer = executor.run(["сделай что-нибудь"], thread_id=TID, system=SYS)
        result["answer"] = strip_volatile(answer) if isinstance(answer, (dict, list)) else answer
        result["error"] = None
    except Exception as exc:                    # noqa: BLE001 — фиксируем ЛЮБОЙ отказ
        # Отказ — тоже наблюдаемое поведение и тоже подлежит сверке: важно, что
        # он ОДИНАКОВ по обе стороны, а не что его нет.
        result["answer"] = None
        result["error"] = f"{type(exc).__name__}: {exc}"

    result["dump"] = run_thread_dump(cfg, SYS, TID)
    result["backend_calls"] = len(getattr(backend, "calls", []))

    try:
        from gc_core.thread_store import load_thread_meta, resolve_threads_dir

        meta = load_thread_meta(resolve_threads_dir(cfg), SYS, TID) or {}
        result["meta"] = strip_volatile({
            k: v for k, v in meta.items()
            if k in ("messages_count", "backend_api_mode", "runtime",
                     "dialog_steps_offset", "user_turn_seq")
        })
    except Exception:                            # noqa: BLE001
        result["meta"] = {}
    return result


def run_all(tmp_root: Path) -> List[Dict[str, Any]]:
    """Прогнать всю матрицу. Каждый случай — со своим ``monkeypatch``."""
    from _pytest.monkeypatch import MonkeyPatch

    out: List[Dict[str, Any]] = []
    for case in ALL_CASES:
        mp = MonkeyPatch()
        try:
            out.append(run_case(case, tmp_root, mp))
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
        raise SystemExit("использование: python toggle_matrix.py <файл-результата>")
    root = Path(tempfile.mkdtemp(prefix="toggle-matrix-"))
    results = run_all(root)
    Path(sys.argv[1]).write_text(
        json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    errors = [r for r in results if r.get("error")]
    print(f"случаев: {len(results)}, с отказом: {len(errors)}", file=sys.stderr)


if __name__ == "__main__":
    main()
