"""One registered tool implementation across local and explicit effect ports."""

import json
from types import SimpleNamespace

import pytest
from gc_core.execution.tool_effects import execute_native_tool_effect
from gc_core.security.guard_probe import evaluate_guard_probe
from gc_core.security.permission_store import PermissionStore
from gc_core.tools import aapply_tool
from gc_core.tools.exec.download_file import DownloadFile
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_testkit.helpers.tool_ctx import local_ctx


def setup_ports(config, tool_class):
    worker = local_ctx(config)
    core = local_ctx(config)
    name = tool_class.registered_tool_name()
    registry = {
        name: {
            "tool_fabric": tool_class,
            "spec": tool_class.tool_description(),
        }
    }
    worker.tools_data = core.tools_data = registry
    core.permission_store = PermissionStore()
    events = []

    async def probe(request):
        events.append(("probe", request.kind))
        return evaluate_guard_probe(request, settings=worker)

    async def human(request):
        events.append(("human", request.decision_key))
        return "allow_once"

    async def effect(request):
        events.append(("effect", request.effect_step))
        return await execute_native_tool_effect(request, ctx=worker)

    core.guard_probe, core.human_request, core.tool_effect = (
        probe,
        human,
        effect,
    )
    return core, worker, events


@pytest.mark.asyncio
async def test_write_runs_guards_and_human_before_leaf(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    dest = tmp_path / "outside.txt"
    ctx, _, events = setup_ports(
        {
            "launch_root": str(project),
            "security.write_guard": {"enabled": True, "mode": "ask"},
        },
        WriteToFile,
    )
    output = await aapply_tool(
        "fs_write_file",
        {"path": str(dest), "content": "hello"},
        ctx=ctx,
        call_id="write-1",
    )
    assert json.loads(output)["ok"] is True
    assert dest.read_text() == "hello"
    assert events[-2:] == [("human", f"write:{dest}"), ("effect", "body")]
    assert ctx.permission_store.session_rules() == {}


@pytest.mark.asyncio
async def test_changed_guard_facts_prevent_effect(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    dest = project / "file.txt"
    ctx, worker, _ = setup_ports(
        {
            "launch_root": str(project),
            "security.write_guard": {"enabled": True},
        },
        WriteToFile,
    )

    async def effect(request):
        request.arguments["path"] = str(tmp_path / "other.txt")
        return await execute_native_tool_effect(request, ctx=worker)

    ctx.tool_effect = effect
    # Как в pydantic-ai: изменившиеся факты — ошибка инструмента для модели,
    # а не сбой хода; эффект при этом не выполняется.
    output = await aapply_tool(
        "fs_write_file", {"path": str(dest), "content": "hello"}, ctx=ctx
    )
    assert "Tool arguments changed after guard evaluation" in output
    assert not dest.exists()


@pytest.mark.asyncio
async def test_download_uses_native_human_before_download_effect(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    ctx, _, events = setup_ports(
        {
            "launch_root": str(project),
            "security.write_guard": {"enabled": True, "mode": "ask"},
            "security.download": {"download_dir": str(tmp_path / "outside")},
        },
        DownloadFile,
    )

    async def http_get(_self, url, _max_bytes):
        events.append(("http", url))
        return b"%PDF-1.4 fake", None

    monkeypatch.setattr(DownloadFile, "_http_get_bytes", http_get)
    output = await aapply_tool(
        "download_file",
        {"url": "https://example.org/a.pdf", "filename": "a.pdf"},
        ctx=ctx,
    )
    assert json.loads(output)["ok"] is True
    assert (tmp_path / "outside/a.pdf").read_bytes() == b"%PDF-1.4 fake"
    assert [event[0] for event in events][-3:] == ["human", "effect", "http"]
    assert ("effect", "download") in events


@pytest.mark.asyncio
async def test_command_judge_human_and_effect_are_separate(
    tmp_path, monkeypatch
):
    import logging

    import gc_core.security.command_moderation as moderation
    from gc_core.tools.exec.execute_command import ExecuteCommand

    ctx, _, events = setup_ports(
        {
            "launch_root": str(tmp_path),
            "backend.completions_url": "https://model.invalid",
            "security.command_moderation": {"enabled": True, "mode": "ask"},
        },
        ExecuteCommand,
    )
    from gc_core.config_modules.chat_completions import (
        auxiliary_llm_from_config_dict,
    )

    ctx.auxiliary_llm = auxiliary_llm_from_config_dict(ctx)
    ctx.logger = logging.getLogger("native_effect_test")

    async def model(**kwargs):
        events.append(("model", kwargs["consumer"]))
        return json.dumps(
            {
                "verdict": "potentially_dangerous",
                "reasons": ["confirm"],
                "risk_flags": ["confirm"],
                "confidence": 0.9,
            }
        )

    monkeypatch.setattr(
        moderation, "arequest_auxiliary_completion_text", model
    )
    output = await aapply_tool(
        "exec_command",
        {"command": "echo native", "cwd": str(tmp_path)},
        ctx=ctx,
        state=SimpleNamespace(shared=SimpleNamespace()),
    )
    assert json.loads(output)["ok"] is True
    phases = [event[0] for event in events if event[0] != "probe"]
    assert phases == ["model", "human", "effect"]
    assert ("probe", "command_prompts") in events


@pytest.mark.asyncio
async def test_fetch_cache_hit_preserves_lru_order_across_effect(tmp_path):
    from collections import OrderedDict

    from gc_core.tools.exec.simple_fetch_url import SimpleFetchUrl

    ctx, _, events = setup_ports(
        {"launch_root": str(tmp_path)}, SimpleFetchUrl
    )
    state = SimpleNamespace(
        shared=SimpleNamespace(
            fetch_url_cache=OrderedDict(
                [
                    ("https://a.example", {"content": "A"}),
                    ("https://b.example", {"content": "B"}),
                ]
            )
        )
    )
    output = await aapply_tool(
        "exec_fetch_url", {"url": "https://a.example"}, ctx=ctx, state=state
    )
    assert json.loads(output)["cache_hit"] is True
    assert list(state.shared.fetch_url_cache) == [
        "https://b.example",
        "https://a.example",
    ]


@pytest.mark.asyncio
async def test_stored_output_reads_live_pure_store_in_core(tmp_path):
    from gc_core.execution.output_store import (
        ToolCallOutputRecord,
        ToolCallOutputsStore,
    )
    from gc_core.tools.session.stored_outputs.read_stored_tool_output import (
        ReadStoredToolOutput,
    )

    ctx, _, events = setup_ports(
        {"launch_root": str(tmp_path)}, ReadStoredToolOutput
    )
    store = ToolCallOutputsStore()
    store.put(ToolCallOutputRecord("ref1", "source", "call1", 5, "hello"))
    state = SimpleNamespace(tool_call_outputs_store=store)
    output = await aapply_tool(
        "session_read_stored_tool_output",
        {"output_ref": "ref1"},
        ctx=ctx,
        state=state,
    )
    assert json.loads(output)["content"] == "hello"
    assert events == []


def _png_bytes():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 24), "blue").save(buf, format="PNG")
    return buf.getvalue()


def _vision_ports(tmp_path, monkeypatch):
    from gc_core.attachments.store import ImageStore
    from gc_core.model_router import ResolvedModel
    from gc_core.tools.fs import view_image as view_image_module
    from gc_core.tools.fs.view_image import ViewImage
    from gc_core.vision.audience import Audience

    project = tmp_path / "project"
    project.mkdir()
    ctx, worker, events = setup_ports({"launch_root": str(project)}, ViewImage)
    worker.image_store = ImageStore(str(tmp_path / "worker-images"))
    worker.model_router = object()
    seen = {}
    native = ResolvedModel(
        alias="vis",
        model="vis-model",
        url="http://backend.local/v1/chat/completions",
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test",
    )

    async def audience(ctx_arg, state):
        # «Кто смотрит» считается на воркере — по срезу хода из workflow.
        seen["probe_turn"] = state.vision_probe_turn
        seen["tools"] = state.tools
        return Audience(native=native, eyes=None, tool_visible=True)

    monkeypatch.setattr(view_image_module, "aensure_view_audience", audience)
    return project, ctx, worker, events, seen


def _workflow_state(**fields):
    from gc_core.constants.tool_names import FS_VIEW_IMAGE_NAME

    base = dict(
        shared=SimpleNamespace(working_memory={}, fetch_url_cache={}),
        session_todo=[],
        messages=[],
        tools=[{"name": FS_VIEW_IMAGE_NAME}],
        vision_probe_turn="turn-1",
        turn_level=None,
        vision_live_ids=frozenset(),
        vision_audience=None,
        pending_tool_attachments=[],
    )
    base.update(fields)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
@pytest.mark.parametrize("level_field", ["_call_level", "turn_level"])
async def test_view_image_worker_probes_the_model_of_the_actual_call(
    tmp_path, monkeypatch, level_field
):
    import httpx
    from gc_core.attachments.store import ImageStore
    from gc_core.model_router import ModelRouter
    from gc_core.tools.fs.view_image import ViewImage
    from gc_core.utils import connect_backend
    from gc_core.vision import probe

    project = tmp_path / "project"
    project.mkdir()
    config = {
        "launch_root": str(project),
        "level": "default",
        "backend.api_mode": "chat_completions",
        "models": {
            name: {
                "model": name,
                "url": f"http://{name}/v1/chat/completions",
                "token": "",
            }
            for name in ("fast", "mid")
        },
        "slots": {"light": "fast", "default": "mid"},
    }
    ctx, worker, _ = setup_ports(config, ViewImage)
    if level_field == "turn_level":
        from dataclasses import asdict
        from gc_core.execution.contracts import ToolVisionView

        async def legacy_effect(request):
            previous_wire = asdict(request.state.vision)
            previous_wire.pop("call_level")
            request.state.vision = ToolVisionView(**previous_wire)
            return await execute_native_tool_effect(request, ctx=worker)

        ctx.tool_effect = legacy_effect
    worker.image_store = ImageStore(str(tmp_path / "images"))
    worker.model_router = ModelRouter(
        worker.settings, level_provider=lambda: "default"
    )
    challenge = probe.make_challenge(retry=False)
    monkeypatch.setattr(probe, "make_challenge", lambda **_: challenge)
    sent = []

    def serve(request):
        sent.append(json.loads(request.content)["model"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": challenge.code,
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(serve)
    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_: httpx.AsyncClient(transport=transport),
    )
    image = project / "shot.png"
    image.write_bytes(_png_bytes())
    state = _workflow_state(**{level_field: "light"})
    output = json.loads(
        await aapply_tool(
            "fs_view_image",
            {"path": str(image)},
            ctx=ctx,
            state=state,
            call_id="view",
        )
    )
    assert output["ok"] is True and output["attached"] is True
    assert sent == ["fast"]


@pytest.mark.asyncio
async def test_view_image_snapshot_crosses_the_effect_boundary(
    tmp_path, monkeypatch
):
    """Снимок кладёт тул на воркере, а в хвост батча он попадает в workflow —
    через ``StateDelta.pending_tool_attachments``, не через общий State."""
    project, ctx, worker, events, seen = _vision_ports(tmp_path, monkeypatch)
    image = project / "shot.png"
    image.write_bytes(_png_bytes())
    state = _workflow_state()

    output = json.loads(
        await aapply_tool(
            "fs_view_image",
            {"path": str(image)},
            ctx=ctx,
            state=state,
            call_id="v1",
        )
    )

    assert output["ok"] is True and output["attached"] is True
    assert events[-1] == ("effect", "body")
    assert seen == {
        "probe_turn": "turn-1",
        "tools": [{"name": "fs_view_image"}],
    }
    assert len(state.pending_tool_attachments) == 1
    snapshot = state.pending_tool_attachments[0]
    from gc_core.attachments.model import Attachment

    stored = worker.image_store.read_for_send(Attachment.from_dict(snapshot))
    assert stored, "файл снимка лежит в хранилище воркера"


@pytest.mark.asyncio
async def test_view_image_names_an_image_already_live_in_the_request(
    tmp_path, monkeypatch
):
    """Картинка уже ушла модели целиком: тул называет её подписью из истории
    и второй снимок не прикладывает."""
    from gc_core.attachments.normalize import normalize_image
    from gc_core.attachments.store import image_id

    project, ctx, _, _, _ = _vision_ports(tmp_path, monkeypatch)
    data = _png_bytes()
    image = project / "shot.png"
    image.write_bytes(data)
    att_id = image_id(normalize_image(data).data)
    state = _workflow_state(
        vision_live_ids=frozenset({att_id}),
        messages=[
            {
                "role": "user",
                "content": "[Изображение #3] вот",
                "attachments": [
                    {
                        "id": att_id,
                        "kind": "image",
                        "mime": "image/png",
                        "width": 32,
                        "height": 24,
                        "size": len(data),
                        "path": "/nowhere.png",
                        "origin": "user",
                        "label": "Изображение #3",
                        "source": "clipboard",
                        "orig_width": 32,
                        "orig_height": 24,
                    }
                ],
            }
        ],
    )

    output = json.loads(
        await aapply_tool(
            "fs_view_image", {"path": str(image)}, ctx=ctx, state=state
        )
    )

    assert output.get("already_in_context") is True
    assert "Изображение #3" in output["message"]
    assert state.pending_tool_attachments == []
