"""MCP-медиа сохраняются туда, где охрана видит класс `media`: каталог один на двоих."""

import base64
from pathlib import Path

from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import mcp_media_dir
from gc_core.security.media_roots import media_roots
from gc_core.tools.connect_mcp import api


def test_mcp_media_dir_is_under_gc_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    assert mcp_media_dir() == tmp_path / "gc" / "cache" / "mcp-media"


async def test_saved_mcp_image_lands_in_a_media_root(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    block = {
        "type": "image",
        "mimeType": "image/png",
        "data": base64.b64encode(b"png").decode(),
    }
    saved = await api._asave_media_block(
        block, {"filename": "shot.png"}, "browser_take_screenshot"
    )
    assert Path(saved["path"]) == mcp_media_dir() / "shot.png"
    assert mcp_media_dir() in media_roots(None)


async def test_mcp_media_goes_to_the_thread_when_the_store_has_no_files(
    tmp_path, monkeypatch
):
    """Хранилище тредов без файлов: медиа из ответа MCP — вложением треда,
    каталога ``mcp-media`` на диске процесса нет; модель получает ссылку,
    по которой ``fs_view_image`` прочтёт байты через хранилище."""
    from types import SimpleNamespace

    from gc_core.attachments.store import StoreImageStore
    from gc_core.thread_files import parse_thread_file_uri
    from gc_testkit.memory_thread_store_v2 import MemoryThreadStore

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    image_store = StoreImageStore(MemoryThreadStore())
    ctx = SimpleNamespace(
        image_store=image_store, system_id="sys", thread_id="t1"
    )
    monkeypatch.setattr(
        "gc_core.thread_paths.thread_address", lambda _ctx: ("sys", "t1")
    )
    block = {
        "type": "image",
        "mimeType": "image/png",
        "data": base64.b64encode(b"png-bytes").decode(),
    }
    with api.media_sink_scope(ctx):
        saved = await api._asave_media_block(
            block, {"filename": "снимок экрана.png"}, "browser_take_screenshot"
        )
    assert parse_thread_file_uri(saved["path"])[:2] == ("sys", "t1")
    assert await image_store.aread_uri(saved["path"]) == b"png-bytes"
    assert not mcp_media_dir().exists()
    # Вне вызова инструмента — как раньше, файлом в mcp-media.
    plain = await api._asave_media_block(block, {"filename": "b.png"}, "tool")
    assert Path(plain["path"]) == mcp_media_dir() / "b.png"
