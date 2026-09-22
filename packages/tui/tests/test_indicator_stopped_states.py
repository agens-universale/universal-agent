from gc_tui.widgets.thinking_indicator import ThinkingIndicator


def _text(state):
    ind = ThinkingIndicator(state=state, clock=lambda: 0.0)
    return ind._render_text().plain


def test_stopped_steps_text():
    assert _text("stopped_steps") == "⏹ Остановлено: превышен лимит шагов"


def test_stopped_errors_text():
    assert _text("stopped_errors") == "⏹ Остановлено: превышен лимит ошибок"
