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


def test_saved_mcp_image_lands_in_a_media_root(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    block = {
        "type": "image",
        "mimeType": "image/png",
        "data": base64.b64encode(b"png").decode(),
    }
    saved = api._save_media_block(
        block, {"filename": "shot.png"}, "browser_take_screenshot"
    )
    assert Path(saved["path"]) == mcp_media_dir() / "shot.png"
    assert mcp_media_dir() in media_roots(None)


def test_mcp_media_goes_to_thread_files_when_the_store_keeps_them(
    tmp_path, monkeypatch
):
    """Хранилище тредов с файлами треда: медиа из ответа MCP — файлом треда,
    каталога ``mcp-media`` на диске процесса нет; модель получает ссылку,
    по которой ``fs_view_image`` прочтёт байты через хранилище."""
    from types import SimpleNamespace

    from gc_core.attachments.store import StoreImageStore
    from gc_core.thread_files import parse_thread_file_uri
    from gc_testkit.memory_thread_store_v2 import (
        MemoryThreadStoreWithFiles,
    )

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    store = MemoryThreadStoreWithFiles()
    image_store = StoreImageStore(store)
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
        saved = api._save_media_block(
            block, {"filename": "shot.png"}, "browser_take_screenshot"
        )
    assert parse_thread_file_uri(saved["path"]) == (
        "sys",
        "t1",
        "mcp-media/shot.png",
    )
    assert image_store.read_uri(saved["path"]) == b"png-bytes"
    # Имя от MCP с кириллицей и пробелом: файл не теряется, имя — безопасное.
    with api.media_sink_scope(ctx):
        odd = api._save_media_block(
            block, {"filename": "снимок экрана.png"}, "browser_take_screenshot"
        )
    _sys, _thread, name = parse_thread_file_uri(odd["path"])
    assert name.startswith("mcp-media/") and name.endswith(".png")
    assert image_store.read_uri(odd["path"]) == b"png-bytes"
    assert not mcp_media_dir().exists()
    # Вне вызова инструмента — как раньше, файлом в mcp-media.
    plain = api._save_media_block(block, {"filename": "b.png"}, "tool")
    assert Path(plain["path"]) == mcp_media_dir() / "b.png"
