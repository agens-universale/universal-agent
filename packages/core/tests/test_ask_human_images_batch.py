"""Картинки ответа на turn_ask_human доходят до модели техническим сообщением батча."""

import io
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from gc_testkit import as_coro

from gc_core import processing_responses as pr
from gc_core.attachments.model import ORIGIN_USER, chip_text, user_label
from gc_core.attachments.normalize import normalize_image
from gc_core.attachments.store import ImageStore
from gc_core.constants.tool_names import ASK_HUMAN_TOOL_NAME
from gc_core.tools.turn.ask_human import AskHuman, HumanAnswer


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(buf, format="PNG")
    return buf.getvalue()


def _state():
    return SimpleNamespace(
        messages=[],
        steps=1,
        max_steps=50,
        session_compact_applied=False,
        tool_calls=0,
        turn_tool_calls=0,
        loop_detector_window=[],
        loop_detector_pending=[],
        last_unknown_tool_name=None,
        unknown_tool_repeat_count=0,
        last_internal_error_tool_name=None,
        internal_error_repeat_count=0,
        last_batch_unknown_tool_count=0,
        last_batch_invalid_arguments_count=0,
        stop_requested=False,
        pending_tool_attachments=[],
    )


async def test_answer_images_ride_the_technical_message_after_the_batch(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        pr, "maybe_store_and_wrap_tool_output", lambda res, **_kw: res
    )
    monkeypatch.setattr(
        pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **_kw: None)
    )
    store = ImageStore(str(tmp_path / "images"))
    att = store.stage(
        normalize_image(_png()),
        origin=ORIGIN_USER,
        label=user_label(3),
        source="clipboard",
    )
    text = f"{chip_text(3)} вот"
    spec = AskHuman.tool_description()
    ctx = SimpleNamespace(
        config_dict={},
        logger=None,
        run_id="run",
        subagent_progress_sink=None,
        tools_data={
            ASK_HUMAN_TOOL_NAME: {
                "tool_fabric": AskHuman,
                "spec": spec,
                "description": "",
            }
        },
        image_store=store,
        system_id="cli",
        thread_id="t1",
        ask_human=lambda q: HumanAnswer(text=text, attachments=(att,)),
    )
    state = _state()
    call = {
        "id": "id_c_ask",
        "type": "function_call",
        "call_id": "c_ask",
        "name": ASK_HUMAN_TOOL_NAME,
        "arguments": json.dumps(
            {"question": "Что не так?"}, ensure_ascii=False
        ),
    }

    fco, tech = await pr.aprocess_tool_calls(
        messages=[call], state=state, ctx=ctx, run_agent_func=None
    )

    assert json.loads(fco["output"])["response"] == text
    assert tech["content"] == (
        '<technical type="technical">'
        "Изображение #3 from the user's answer to turn_ask_human · png 64×48"
        "</technical>"
    )
    [sent] = tech["attachments"]
    assert Path(sent["path"]) == tmp_path / "images" / f"cli_t1_{att.id}.png"
    assert tech["id"].startswith("m_")
    assert state.pending_tool_attachments == []
