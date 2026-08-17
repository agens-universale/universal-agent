"""ThinkingIndicator: состояние compressing → фикс-текст «сжимаю контекст…»."""
from gc_tui.widgets.thinking_indicator import ThinkingIndicator


def test_compressing_state_renders_fixed_text():
    ind = ThinkingIndicator(state="compressing")
    txt = ind._render_text().plain
    assert "сжимаю контекст" in txt
    assert "с)" in txt  # elapsed присутствует


def test_set_state_accepts_compressing():
    ind = ThinkingIndicator(state="thinking")
    ind.set_state("compressing")
    assert ind._state == "compressing"
