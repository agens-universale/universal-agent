"""InputHistory не хранит слэш-команды — ни при append, ни при load."""
import json

from tui.services.input_history import InputHistory


def test_append_skips_commands(tmp_path):
    h = InputHistory(tmp_path)
    h.append("привет")
    h.append("/init")          # команда — пропускается
    h.append("/status")        # команда — пропускается
    h.append("что это за проект")
    assert h.size == 2
    # навигация не доходит до команд
    texts = []
    idx = None
    while True:
        step = h.prev(idx)
        if step is None:
            break
        idx, text = step
        texts.append(text)
    assert "/init" not in texts and "/status" not in texts
    assert "привет" in texts and "что это за проект" in texts


def test_load_filters_old_command_entries(tmp_path):
    # старый файл с command-записями (от прежнего поведения)
    f = tmp_path / InputHistory.HISTORY_FILENAME
    lines = [
        {"ts": "t", "text": "привет"},
        {"ts": "t", "text": "/init"},
        {"ts": "t", "text": "/status"},
        {"ts": "t", "text": "проанализируй проект"},
    ]
    f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")
    h = InputHistory(tmp_path)
    h.load()
    assert h.size == 2   # команды отсеяны при загрузке
