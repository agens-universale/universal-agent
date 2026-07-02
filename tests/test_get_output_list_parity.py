from utils.convertations import get_output_list
from utils.chat_completions_stream import assemble_streamed_completion


def test_stream_assembly_and_direct_json_give_same_output():
    # «non-stream» dict
    direct = {
        "id": "r1", "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "привет"},
                     "finish_reason": "stop"}],
    }
    # тот же ответ, собранный из SSE-дельт
    assembled = assemble_streamed_completion([
        '{"id":"r1","model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":"при"},"finish_reason":null}]}',
        '{"choices":[{"index":0,"delta":{"content":"вет"},"finish_reason":"stop"}]}',
    ])
    out_direct = get_output_list(direct)
    out_assembled = get_output_list(assembled)
    # оба дают одинаковый message-output (единый путь валидации)
    assert [o["type"] for o in out_direct] == [o["type"] for o in out_assembled]
    assert out_direct[0]["content"][0]["text"] == out_assembled[0]["content"][0]["text"] == "привет"
