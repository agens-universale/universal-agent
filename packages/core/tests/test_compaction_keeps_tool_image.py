"""Картинка тула, записанная при отказе судьи финала, остаётся живой после
сводки.

Модель одним батчем зовёт тул с картинкой и ``turn_finish``; судья
отклоняет, и хвост батча с картинкой уходит в тред уже на этом шаге. Пока
батч исполнялся, человек дослал реплику. Следующий шаг несёт прошлый
батч и реплику, и перед запросом срабатывает сжатие. Сводка картинку не
несёт, поэтому после неё картинка обязана вернуться рядом с репликой — один
раз, без дубля в треде.
"""

import io
import json
from copy import deepcopy

from gc_core import processing_responses as pr
from gc_core import run_summary_coordinator as rsc
from gc_core.agent_executor import AgentExecutor
from gc_core.attachments.model import ORIGIN_TOOL, TOOL_LABEL, Attachment
from gc_core.attachments.store import image_id
from gc_core.run_context import acreate_run_context
from gc_core.summarize.compaction import SummaryTriggerHits
from parity_harness import (
    ScriptedBackend,
    assistant_text,
    assistant_tool_call,
    install,
    judge_accept,
    parity_config,
)
from PIL import Image
from vision_fakes import warm_routes

_REPLY = "второй вопрос про картинку"
_REJECT = (
    '{"action":"fix_report_and_continue","trace_ok":false,'
    '"report_ok":false,"is_question":false,"trace_gaps":["Проверь"]}'
)


def _finish(report, call_id):
    return assistant_tool_call(
        "turn_finish",
        {
            "solution_type": "full_solution",
            "task": "Посчитать",
            "task_report": report,
            "handoff": "{}",
        },
        call_id,
    )


def _one_batch(*turns):
    """Ответ модели со всеми вызовами ``turns`` одним батчем."""
    response = deepcopy(turns[0])
    response["choices"][0]["message"]["tool_calls"] = [
        call
        for turn in turns
        for call in turn["choices"][0]["message"]["tool_calls"]
    ]
    return response


def _image(tmp_path):
    path = tmp_path / "shot.png"
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return Attachment(
        id=image_id(buf.getvalue()),
        kind="image",
        mime="image/png",
        width=64,
        height=48,
        size=path.stat().st_size,
        path=str(path),
        origin=ORIGIN_TOOL,
        label=TOOL_LABEL,
        source=str(path),
        orig_width=64,
        orig_height=48,
    ).to_dict()


def _compact_once_reply_arrives(monkeypatch):
    """Порог сжатия срабатывает один раз — когда реплика уже в истории;
    сводку строит настоящая компакция с подменённым суммаризатором."""
    fired = []

    def _hits(self):
        if fired or not any(
            m.get("content") == _REPLY for m in self._state.messages
        ):
            return None
        fired.append(True)
        return SummaryTriggerHits(chars=True)

    async def _summarize(**_kwargs):
        return {
            "raw_summary": "<task_and_context>итог</task_and_context>",
            "num_batches": 1,
            "last_dialog_steps": "",
            "trace": {"memory_patch": {"executed": False}},
        }

    monkeypatch.setattr(
        rsc.RunSummaryCoordinator, "_backend_batch_trigger_hits", _hits
    )
    monkeypatch.setattr(
        "gc_core.summarize.compaction.asummarize_dialog_via_chat_completions",
        _summarize,
    )
    return fired


def _image_tool_with_queued_reply(monkeypatch, image, executor_ref):
    """Калькулятор как тул с картинкой: после него в ``state`` лежит
    картинка, как после ``fs_view_image``, а в очереди — реплика."""
    real = pr.aapply_tool

    async def _apply(**kw):
        out = await real(**kw)
        if kw.get("tool_name") == "exec_calculator":
            kw["state"].pending_tool_attachments.append(dict(image))
            executor_ref[0].add_message(
                {"role": "user", "content": _REPLY, "id": "m_reply"}
            )
        return out

    monkeypatch.setattr(pr, "aapply_tool", _apply)


def _summary_request(backend):
    """Первый запрос главного цикла после сводки: промпт, сводка, живое."""
    return next(
        call["messages"]
        for call in backend.calls
        if len(call["messages"]) > 1
        and "dialog_summary" in str(call["messages"][1].get("content"))
    )


def _live_thread_rows(tmp_path):
    (data,) = [
        f
        for f in (tmp_path / "threads").rglob("data_*.jsonl")
        if "__pre_summary__" not in f.name
    ]
    rows = [json.loads(line) for line in data.read_text().splitlines()]
    return [row.get("payload", row) for row in rows]


async def test_tool_image_survives_compaction_after_finish_reject(
    tmp_path, monkeypatch
):
    image = _image(tmp_path)
    fired = _compact_once_reply_arrives(monkeypatch)
    executor_ref = [None]
    _image_tool_with_queued_reply(monkeypatch, image, executor_ref)
    calc = assistant_tool_call(
        "exec_calculator", {"expression": "2+2"}, "calc"
    )
    backend = install(
        monkeypatch,
        ScriptedBackend(
            [
                _one_batch(calc, _finish("Пока не проверено", "f1")),
                assistant_text(_REJECT),
                _finish("Проверено", "f2"),
                judge_accept(),
            ]
        ),
    )
    cfg = parity_config(
        tmp_path, monkeypatch, **{"agent.strict_turn_finish_validation": True}
    )
    ctx = await acreate_run_context(cfg)
    warm_routes(ctx)
    executor_ref[0] = AgentExecutor(ctx)

    await executor_ref[0].arun(["первый вопрос"], thread_id="t1")

    assert fired
    backend.assert_clean()
    live = _summary_request(backend)[2:]
    assert [m["role"] for m in live] == ["user", "user"]
    assert any(part["type"] == "image_url" for part in live[0]["content"])
    assert live[1]["content"] == _REPLY
    images = [r for r in _live_thread_rows(tmp_path) if r.get("attachments")]
    assert [r["attachments"][0]["id"] for r in images] == [image["id"]]
