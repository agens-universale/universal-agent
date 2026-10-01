"""Субагент смотрит картинки через хранилище родителя.

Снимок `fs_view_image` ребёнка ложится в файл треда родителя: ребёнок пишет в
трассу родителя, и запросы родителя проецируют этот снимок через то же
хранилище. Проверка поведенческая — настоящие `acreate_run_context` и
`_build_child_ctx` (структурный сторож полей — `test_child_ctx_inherits_thread_store.py`).
"""

import io
import json
import os
from types import SimpleNamespace

from PIL import Image

from gc_core.model_router import ResolvedModel
from gc_core.run_context import acreate_run_context
from gc_core.tools.fs.view_image import ViewImage
from gc_core.tools.subagents.runner import SubagentRunner
from gc_core.tools.subagents.sidecar import bind_thread_identity
from gc_core.vision.audience import Audience
from vision_fakes import warm_routes

SEEING = Audience(
    native=ResolvedModel(
        alias="vis",
        model="vis-model",
        url=None,
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test",
    ),
    eyes=None,
    tool_visible=True,
)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(buf, format="PNG")
    return buf.getvalue()


async def test_child_view_image_snapshot_lands_in_the_parent_store(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    threads = tmp_path / "threads"
    config = {
        "logs_dump_dir": str(tmp_path / "logs"),
        "console_trace": False,
        "backend_api_mode": "chat_completions",
        "completions_url": "http://localhost:1/v1/chat/completions",
        "model": "test-model",
        "backend_request_timeout_sec": 600,
        "launch_root": str(proj),
        "threads_data_path": str(threads),
        "use_local_subagents": True,
    }
    parent = await acreate_run_context(config)
    bind_thread_identity(
        parent, threads_dir=str(threads), system_id="cli", thread_id="t1"
    )
    # _build_child_ctx читает только self._ctx и self._config — state здесь заглушка.
    child = await SubagentRunner(
        parent, SimpleNamespace(), config
    )._build_child_ctx("subagent_coder")

    assert child.image_store is parent.image_store

    src = proj / "shot.png"
    src.write_bytes(_png())
    state = SimpleNamespace(
        vision_audience=SEEING,
        pending_tool_attachments=[],
        current_user_intent=[],
    )
    state.vision_probe_turn = "root-test"
    state.state_id = "child-test"
    state.x_target = None
    warm_routes(child)
    out = json.loads(
        await ViewImage(
            ctx=child, state=state, config_dict=child.config_dict
        ).acall(path=str(src))
    )

    assert out["attached"] is True
    [att] = state.pending_tool_attachments
    assert att["path"] == os.path.join(
        parent.image_store.root, f"cli_t1_{att['id']}.png"
    )
    assert os.path.isfile(att["path"])
