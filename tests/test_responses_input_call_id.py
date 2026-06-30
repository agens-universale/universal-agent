from types import SimpleNamespace
from utils.connect_backend import create_call_data_responses

def test_responses_input_strips_extra_call_id_from_message_items():
    state = SimpleNamespace(default_model="m", tools=[], call_metadata={},
                            previous_response_id=None)
    msgs = [
        {"role": "user", "content": "x", "id": "m1", "call_id": "call_1"},
        {"type": "function_call", "call_id": "call_2", "name": "f", "arguments": "{}"},
    ]
    data = create_call_data_responses(msgs, state)
    assert "call_id" not in data["input"][0]      # снят с user-сообщения
    assert data["input"][1]["call_id"] == "call_2"  # на function_call сохранён
    assert msgs[0]["call_id"] == "call_1"          # исходник НЕ мутирован (TUI/persist)
