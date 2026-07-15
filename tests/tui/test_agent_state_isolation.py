"""I3: TUI-тесты не должны трогать реальный ~/.gc/agent_state.json."""
import pytest

pytest.importorskip("textual")


def test_autouse_state_path_redirects_to_tmp(tmp_path):
    """Автоuse-фикстура conftest._isolate_agent_state редиректит
    upgrade_gate._state_path на tmp_path для ЛЮБОГО TUI-теста — без неё
    upgrade_gate писал бы в реальный конфиг-каталог пользователя
    (env_vars.effective_config_dir()), что и происходило до фикса."""
    import upgrade_gate
    assert upgrade_gate._state_path().parent == tmp_path
