"""S2/A3: TUI-only спец-инструкции содержат принципы (дедуп + 3-way вопросы)."""
from tui.services.prompt_assets import tui_special_instructions_path


def test_special_instructions_anchors_present():
    text = open(tui_special_instructions_path(), encoding="utf-8").read()
    assert "State the answer once" in text          # дедуп-принцип
    assert "forward-looking, non-blocking" in text  # открытый вопрос в финале разрешён
    assert "opens no input" in text                 # причина «не спрашивай прозой»
