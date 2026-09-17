import os
from gc_tui.services.prompt_assets import tui_special_instructions_path


def test_path_points_to_existing_tui_instructions_file():
    p = tui_special_instructions_path()
    assert p.endswith("chat_completions_tui_special_instructions.md")
    assert os.path.isfile(p)
