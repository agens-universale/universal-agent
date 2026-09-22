"""Хвост прогона целиком достаётся владельцу, и из него собирается отдельный ход.

`finally` прогона снимает несъеденное с очереди в `_undelivered_messages`, но
после этого прогон ещё закрывает MCP-сессии — через `await`, и не быстро. Всё,
что легло в очередь в это окно, оставалось в `_pending_messages`:
`take_undelivered_messages` его не видел, а следующий прогон подмешивал такое
сообщение ПОЗАДИ своего запроса. Сервер к тому же строит на каждый старт новый
исполнитель — там оно пропадало совсем.

Каркас как в `test_executor_queue_before_loop.py`: настоящие
`acreate_run_context` и `AgentExecutor.arun`, подменены только цикл агента и
снос MCP (двойник кладёт сообщение ровно в окно сноса).
"""

import importlib
import types


def _cfg(tmp_path):
    return {
        "threads_data_path": str(tmp_path),
        "backend_api_mode": "chat_completions",
        "completions_url": "http://test",
        "model": "x",
        "max_steps": 1,
        "system_prompt_id": "v1",
    }


async def _executor(monkeypatch, tmp_path, seen: list, on_teardown):
    from gc_core.run_context import acreate_run_context

    ae = importlib.import_module("gc_core.agent_executor")
    monkeypatch.setattr(ae, "gc", types.SimpleNamespace(collect=lambda: None))

    async def _loop(messages, state, ctx, **kw):
        seen.append([m.get("content") if isinstance(m, dict) else m for m in messages])
        return {"finished": True}

    original_teardown = ae._aclose_mcp_and_release

    async def _teardown(owner_reset):
        on_teardown()
        await original_teardown(owner_reset)

    monkeypatch.setattr(ae, "arun_agent", _loop)
    monkeypatch.setattr(ae, "_aclose_mcp_and_release", _teardown)
    ctx = await acreate_run_context(_cfg(tmp_path), auth_connector=None)
    return ae.AgentExecutor(ctx)


async def test_message_queued_during_teardown_is_part_of_the_tail(monkeypatch, tmp_path):
    seen: list = []
    holder: dict = {}

    def _during_teardown():
        holder["ex"].add_message({"role": "user", "content": "в окно сноса", "id": "m_late"})

    ex = await _executor(monkeypatch, tmp_path, seen, _during_teardown)
    holder["ex"] = ex

    await ex.arun(["первый"], thread_id="t", start_new_thread=True, system="cli")

    tail = ex.take_undelivered_messages()
    assert [m["content"] for m in tail] == ["в окно сноса"], (
        f"сообщение, легшее в очередь во время сноса MCP, не попало в хвост: {tail}"
    )
    assert ex.take_undelivered_messages() == [], "хвост отдан, но остался в очереди"


def test_tail_becomes_a_turn_with_the_same_ids_and_display_fields():
    """Из хвоста собирается ход с теми же полями, что у обычного запроса."""
    from gc_core.agent_executor import undelivered_as_arun_args

    texts, extra = undelivered_as_arun_args(
        [
            {"role": "user", "content": "текст", "id": "m_1"},
            {
                "role": "user",
                "content": "обёртка навыка",
                "id": "m_2",
                "display_content": "чистый текст",
                "skill_loaded": "review",
            },
        ]
    )

    assert texts == ["текст", "обёртка навыка"]
    assert extra == {
        "message_ids": ["m_1", "m_2"],
        "display_contents": [None, "чистый текст"],
        "skills_loaded": [None, "review"],
    }
