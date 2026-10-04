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
    assert mcp_media_dir() in media_roots({})
