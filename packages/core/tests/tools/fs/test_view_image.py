"""fs_view_image: чтение, нормализация, копия в тред, кандидат «модель видит сама» (спека §9.2)."""

import io
import json
from pathlib import Path
from types import SimpleNamespace

from gc_core.attachments.model import ORIGIN_TOOL, TOOL_LABEL
from gc_core.attachments.store import ImageStore
from gc_core.attachments.texts import (
    TOOL_ALREADY_IN_CONTEXT,
    TOOL_ATTACHED,
    TOOL_NO_IMAGE_STORE,
    TOOL_NO_VISION,
    TOOL_TOO_MANY_IN_STEP,
)
from gc_core.constants.tool_names import FS_VIEW_IMAGE_NAME
from gc_core.execution_workspace import build_execution_workspace
from gc_core.model_router import ResolvedModel
from gc_core.tools.fs import view_image as view_image_module
from gc_core.tools.fs.view_image import ViewImage
from gc_core.vision.audience import Audience
from gc_core.vision.errors import ERR_NO_IMAGE_STORE, ERR_TOO_MANY_IMAGES
from gc_testkit.harbor_fakes import FakeBackend
from gc_testkit.helpers.tool_ctx import local_ctx
from gc_testkit.settings import fake_ctx, make_settings
from PIL import Image


def _png(size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, format="PNG")
    return buf.getvalue()


def _resolved(alias: str = "vis", model: str = "vis-model") -> ResolvedModel:
    return ResolvedModel(
        alias=alias,
        model=model,
        url="http://backend.local/v1/chat/completions",
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test",
    )


NATIVE = Audience(native=_resolved(), eyes=None, tool_visible=True)
NOBODY = Audience(native=None, eyes=None, tool_visible=False)


def _state(audience=NATIVE):
    return SimpleNamespace(
        vision_audience=audience,
        pending_tool_attachments=[],
        current_user_intent=[],
    )


def _local_tool(tmp_path, *, state, config=None, thread=True):
    cfg = dict(config or {})
    ctx = local_ctx(cfg)
    ctx.image_store = ImageStore(str(tmp_path / "images"))
    ctx.logger = None
    if thread:
        ctx.system_id, ctx.thread_id = "cli", "t1"
    return ViewImage(ctx=ctx, state=state)


class _BytesBackend(FakeBackend):
    """Контейнер с двоичными файлами: ``FakeBackend`` хранит только текст."""

    def __init__(self, blobs):
        super().__init__(dict.fromkeys(blobs, ""))
        self._blobs = dict(blobs)

    async def getsize(self, p):
        return len(self._blobs[p])

    async def read_bytes(self, p):
        return self._blobs[p]


def _sandbox_tool(tmp_path, backend, *, state):
    # Периметр чтения выключен, как в плоском словаре теста на develop (секции
    # `read_guard` там не было): здесь проверяется выбор хоста или контейнера.
    cfg = make_settings(
        {"launch_root": "/work", "security.read_guard": {"enabled": False}}
    )
    ctx = fake_ctx(
        loaded=cfg,
        os_backend=backend,
        workspace=build_execution_workspace(backend, cfg),
        image_store=ImageStore(str(tmp_path / "store")),
        system_id="cli",
        thread_id="t1",
        logger=None,
    )
    return ViewImage(ctx=ctx, state=state)


def test_schema_path_required_question_optional_mcp_hint():
    spec = ViewImage.tool_description()
    assert spec["name"] == FS_VIEW_IMAGE_NAME
    assert spec["parameters"]["required"] == ["path"]
    assert set(spec["parameters"]["properties"]) == {"path", "question"}
    # Скриншот MCP — путь из блока картинки результата, а не из текста MCP.
    assert '"type": "image"' in spec["description"]
    # Приложенную картинку модель уже видит — тул для неё не нужен (смоук 2026-10-01).
    assert "already in front of you" in spec["description"]


async def test_seeing_model_gets_the_image_attached_after_the_batch(tmp_path):
    src = tmp_path / "shot.png"
    src.write_bytes(_png())
    state = _state()

    out = json.loads(
        await _local_tool(tmp_path, state=state).acall(path=str(src))
    )

    assert out == {
        "ok": True,
        "attached": True,
        "path": str(src),
        "width": 64,
        "height": 48,
        "message": TOOL_ATTACHED,
    }
    [att] = state.pending_tool_attachments
    assert att["origin"] == ORIGIN_TOOL
    assert att["label"] == TOOL_LABEL
    assert att["source"] == str(src)
    assert Path(att["path"]) == tmp_path / "images" / f"cli_t1_{att['id']}.png"
    assert Path(att["path"]).is_file()


async def test_one_step_attaches_no_more_snapshots_than_stay_live(tmp_path):
    """В запрос уходят `images.live_tool_images` последних снимков: лишний снимок
    одного шага модель не увидела бы, а «attached» звало бы смотреть его снова."""
    state = _state()
    tool = _local_tool(
        tmp_path, state=state, config={"images": {"live_tool_images": 2}}
    )
    outs = []
    for color in ("red", "green", "blue"):
        src = tmp_path / f"{color}.png"
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), color).save(buf, format="PNG")
        src.write_bytes(buf.getvalue())
        outs.append(json.loads(await tool.acall(path=str(src))))
    assert [o["ok"] for o in outs] == [True, True, False]
    assert outs[2]["error"] == {
        "type": ERR_TOO_MANY_IMAGES,
        "message": TOOL_TOO_MANY_IN_STEP.format(n=2),
        "limit": 2,
    }
    assert len(state.pending_tool_attachments) == 2


def _color_png(tmp_path, color: str) -> Path:
    src = tmp_path / f"{color}.png"
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="PNG")
    src.write_bytes(buf.getvalue())
    return src


async def test_refused_snapshot_is_not_stored(tmp_path):
    """Отказ по лимиту шага не оставляет в треде файла, на который никто не
    ссылается (ревью 2026-10-01)."""
    state = _state()
    tool = _local_tool(
        tmp_path, state=state, config={"images": {"live_tool_images": 1}}
    )
    await tool.acall(path=str(_color_png(tmp_path, "red")))
    out = json.loads(await tool.acall(path=str(_color_png(tmp_path, "green"))))
    assert out["ok"] is False
    assert len(list((tmp_path / "images").glob("cli_t1_*"))) == 1


async def test_same_image_twice_in_a_step_takes_one_place(tmp_path):
    state = _state()
    tool = _local_tool(
        tmp_path, state=state, config={"images": {"live_tool_images": 2}}
    )
    red = str(_color_png(tmp_path, "red"))
    outs = [
        json.loads(await tool.acall(path=path))
        for path in (red, red, str(_color_png(tmp_path, "blue")))
    ]
    assert [o["ok"] for o in outs] == [True, True, True]
    assert len(state.pending_tool_attachments) == 2


async def test_repeats_take_no_slot_even_at_the_limit(tmp_path):
    """Повтор той же картинки в шаге и уже живая картинка места не занимают — лимит
    шага им не отказывает (третье ревью 2026-10-01)."""
    state = _state()
    tool = _local_tool(
        tmp_path, state=state, config={"images": {"live_tool_images": 1}}
    )
    red = str(_color_png(tmp_path, "red"))
    first = json.loads(await tool.acall(path=red))
    again = json.loads(await tool.acall(path=red))
    assert (first["ok"], again["ok"]) == (True, True)
    assert again["attached"] is True
    state.vision_live_ids = frozenset(
        {state.pending_tool_attachments[0]["id"]}
    )
    live = json.loads(await tool.acall(path=red))
    assert live["already_in_context"] is True


async def test_context_without_an_image_store_is_refused(tmp_path):
    """Контекст, собранный в обход фабрики, хранилища не имеет — честный отказ, а
    не внутренняя ошибка тула."""
    src = tmp_path / "shot.png"
    src.write_bytes(_png())
    tool = _local_tool(tmp_path, state=_state())
    tool._ctx.image_store = None
    out = json.loads(await tool.acall(path=str(src)))
    assert out["ok"] is False
    assert out["error"] == {
        "type": ERR_NO_IMAGE_STORE,
        "message": TOOL_NO_IMAGE_STORE,
    }


async def test_an_image_already_sent_in_this_request_is_not_attached_again(
    tmp_path,
):
    """Модель видит сама, а эта картинка уже ушла ей целиком в этом запросе: второй
    снимок нёс бы вторую копию base64 на каждом шаге, пока она живая. Тул отвечает,
    где картинка, и ничего не прикладывает."""
    src = tmp_path / "shot.png"
    src.write_bytes(_png())
    state = _state()
    tool = _local_tool(tmp_path, state=state)
    await tool.acall(path=str(src))
    [att] = state.pending_tool_attachments
    state.pending_tool_attachments = []
    state.messages = [
        {"role": "user", "content": "снимок", "attachments": [att]}
    ]
    state.vision_live_ids = frozenset({att["id"]})

    out = json.loads(
        await tool.acall(path=str(src), question="что за кнопка?")
    )

    assert out == {
        "ok": True,
        "already_in_context": True,
        "path": str(src),
        "message": TOOL_ALREADY_IN_CONTEXT.format(label=TOOL_LABEL),
    }
    assert state.pending_tool_attachments == []


async def test_an_image_that_is_not_live_is_attached_as_usual(tmp_path):
    """Живые в запросе — другие картинки: эту модель ещё не видит, снимок прикладывается."""
    src = tmp_path / "shot.png"
    src.write_bytes(_png())
    state = _state()
    state.vision_live_ids = frozenset({"0" * 16})

    out = json.loads(
        await _local_tool(tmp_path, state=state).acall(path=str(src))
    )

    assert out["attached"] is True
    assert len(state.pending_tool_attachments) == 1


async def test_run_without_thread_keeps_the_copy_in_the_process_staging(
    tmp_path,
):
    src = tmp_path / "shot.png"
    src.write_bytes(_png())
    state = _state()
    tool = _local_tool(tmp_path, state=state, thread=False)

    await tool.acall(path=str(src))

    [att] = state.pending_tool_attachments
    assert Path(att["path"]).parent == Path(tool._ctx.image_store.staging_dir)


async def test_big_screenshot_is_normalized_before_it_reaches_the_model(
    tmp_path,
):
    src = tmp_path / "big.png"
    src.write_bytes(_png((3000, 1000)))
    state = _state()

    out = json.loads(
        await _local_tool(tmp_path, state=state).acall(path=str(src))
    )

    assert out["width"] == 1568
    assert state.pending_tool_attachments[0]["orig_width"] == 3000


async def test_missing_file_is_not_found(tmp_path):
    state = _state()
    out = json.loads(
        await _local_tool(tmp_path, state=state).acall(
            path=str(tmp_path / "nope.png")
        )
    )
    assert out["ok"] is False
    assert out["error"]["type"] == "not_found"
    assert state.pending_tool_attachments == []


async def test_text_file_is_not_an_image(tmp_path):
    src = tmp_path / "notes.png"
    src.write_text("просто текст", encoding="utf-8")
    state = _state()
    out = json.loads(
        await _local_tool(tmp_path, state=state).acall(path=str(src))
    )
    assert out["error"]["type"] == "not_an_image"
    assert state.pending_tool_attachments == []


async def test_file_over_the_byte_ceiling_is_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(view_image_module, "MAX_INPUT_BYTES", 10)
    src = tmp_path / "shot.png"
    src.write_bytes(_png())
    out = json.loads(
        await _local_tool(tmp_path, state=_state()).acall(path=str(src))
    )
    assert out["error"]["type"] == "too_large"


async def test_path_outside_the_read_perimeter_is_read_denied(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    src = outside / "shot.png"
    src.write_bytes(_png())
    cfg = {
        "launch_root": str(project),
        "security.read_guard": {
            "enabled": True,
            "mode": "deny",
            "allowed_read_roots": [str(project)],
        },
    }
    state = _state()

    out = json.loads(
        await _local_tool(tmp_path, state=state, config=cfg).acall(
            path=str(src)
        )
    )

    assert out["ok"] is False
    assert out["error"]["type"] == "read_denied"
    assert state.pending_tool_attachments == []


async def test_nobody_to_look_answers_no_vision_model_without_reading(
    tmp_path,
):
    for audience in (None, NOBODY):
        state = _state(audience)
        out = json.loads(
            await _local_tool(tmp_path, state=state).acall(
                path=str(tmp_path / "nope.png")
            )
        )
        assert out["error"] == {
            "type": "no_vision_model",
            "message": TOOL_NO_VISION,
        }
        assert state.pending_tool_attachments == []


async def test_media_path_is_read_from_the_host_not_from_the_container(
    tmp_path, monkeypatch
):
    media_dir = tmp_path / "mcp-media"
    media_dir.mkdir()
    shot = media_dir / "browser_screenshot_1.png"
    shot.write_bytes(_png())
    monkeypatch.setattr(
        view_image_module, "media_roots", lambda _cfg: (media_dir,)
    )
    state = _state()
    # В контейнере этого файла нет: чтение через os_backend дало бы not_found.
    tool = _sandbox_tool(tmp_path, _BytesBackend({}), state=state)

    out = json.loads(await tool.acall(path=str(shot)))

    assert out["attached"] is True
    assert len(state.pending_tool_attachments) == 1


async def test_project_file_is_read_through_the_execution_environment(
    tmp_path,
):
    state = _state()
    tool = _sandbox_tool(
        tmp_path, _BytesBackend({"/work/shot.png": _png()}), state=state
    )

    out = json.loads(await tool.acall(path="/work/shot.png"))

    assert out["attached"] is True
    assert state.pending_tool_attachments[0]["source"] == "/work/shot.png"


async def test_thread_file_link_is_read_through_the_thread_store(tmp_path):
    """Картинка из ответа MCP в хранилище тредов (ссылка ``thread-file://``):
    тул читает байты через хранилище, снимок кладёт туда же — на диске
    процесса ни файла, ни каталога."""
    from gc_core.attachments.store import StoreImageStore
    from gc_core.thread_files import is_thread_file_uri
    from gc_testkit.memory_thread_store import MemoryThreadStore

    store = MemoryThreadStore()
    image_store = StoreImageStore(store)
    link = image_store.write_media("cli", "t1", "shot.png", _png())
    state = _state()
    tool = _local_tool(tmp_path, state=state)
    tool._ctx.image_store = image_store

    out = json.loads(await tool.acall(path=link))

    assert out["ok"] is True and out["attached"] is True
    assert out["path"] == link
    [att] = state.pending_tool_attachments
    assert is_thread_file_uri(att["path"])
    assert not (tmp_path / "images").exists()
    missing = json.loads(
        await tool.acall(path="thread-file://cli/t1/mcp-media/none.png")
    )
    assert missing["ok"] is False
