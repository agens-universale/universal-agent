"""Секции поверхностей сервера ушли из config.yml в config_server.yml: старая
секция не ломает агента, а называет новое место."""

from gc_core.config import load_settings


def test_chat_api_and_a2a_sections_name_their_new_place(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(
        "chat_api:\n  port: 9001\na2a:\n  port: 9002\n", encoding="utf-8"
    )
    loaded = load_settings(str(path))
    notices = "\n".join(loaded.runtime.notices)
    assert "config_server.yml: chat" in notices
    assert "config_server.yml: a2a" in notices
    assert not hasattr(loaded.settings, "chat_api")
    assert not hasattr(loaded.settings, "a2a")
