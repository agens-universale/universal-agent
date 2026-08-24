import httpx

from gc_core.utils.backend_errors import EmbeddedChoiceError, is_auth_error, is_context_overflow_error, is_empty_completion, is_retriable_backend_error, normalize_finish_reason
from gc_core.utils.chat_completions_stream import BackendStreamError


def _resp(*, content="", tool_calls=None, finish_reason=None, error=None, top_error=None):
    ch = {"index": 0, "message": {"role": "assistant", "content": content},
          "finish_reason": finish_reason}
    if tool_calls is not None:
        ch["message"]["tool_calls"] = tool_calls
    if error is not None:
        ch["error"] = error
    dct = {"choices": [ch]}
    if top_error is not None:
        dct["error"] = top_error
    return dct



def _http_status_error(status: int) -> httpx.HTTPStatusError:
    """HTTP-ошибка в форме httpx: requests ушёл из зависимостей ядра."""
    request = httpx.Request("POST", "http://backend.invalid/v1")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("http error", request=request, response=response)


def test_empty_completion_true_when_nothing():
    assert is_empty_completion(_resp()) is True

def test_empty_completion_no_choices_is_empty():
    assert is_empty_completion({"choices": []}) is True
    assert is_empty_completion({}) is True

def test_not_empty_with_content():
    assert is_empty_completion(_resp(content="hi")) is False

def test_not_empty_with_tool_calls():
    assert is_empty_completion(_resp(tool_calls=[{"id": "c1", "type": "function"}])) is False

def test_not_empty_with_finish_reason():
    assert is_empty_completion(_resp(finish_reason="stop")) is False

def test_not_empty_with_error():
    assert is_empty_completion(_resp(error={"code": 400})) is False
    assert is_empty_completion(_resp(top_error={"code": 500})) is False


def test_detects_gigacode_context_overflow():
    exc = ValueError(
        "Backend stream error: {'object': 'error', 'message': \"Requested token "
        "count exceeds the model's maximum context length of 131072 tokens. You "
        "requested a total of 131479 tokens. Please reduce the number of tokens\", "
        "'type': 'BadRequestError', 'code': 400}"
    )
    assert is_context_overflow_error(exc) is True


def test_detects_vllm_context_overflow():
    # Сырой vLLM формулирует иначе, чем gigacode: «is longer than the model's
    # context length», без «maximum»/«reduce the number of tokens».
    exc = ValueError(
        "Backend stream error: {'object': 'error', 'message': \"The input "
        "(133570 tokens) is longer than the model's context length (131072 "
        "tokens).\", 'type': 'BadRequestError', 'param': None, 'code': 400}"
    )
    assert is_context_overflow_error(exc) is True


def test_ignores_unrelated_backend_errors():
    assert is_context_overflow_error(ValueError("400 Bad Request: temperature invalid type")) is False
    assert is_context_overflow_error(ValueError("Unknown finish reason")) is False
    assert is_context_overflow_error(RuntimeError("401 Unauthorized")) is False


def test_retriable_stream_400():
    assert is_retriable_backend_error(BackendStreamError("x", status_code=400)) is True

def test_retriable_stream_auth_false():
    for code in (401, 403, 404):
        assert is_retriable_backend_error(BackendStreamError("x", status_code=code)) is False

def test_retriable_stream_none_code_true():
    assert is_retriable_backend_error(BackendStreamError("x")) is True

def test_retriable_stream_nonnumeric_false():
    assert is_retriable_backend_error(BackendStreamError("x", status_code="content_filter")) is False

def test_retriable_http_500():
    assert is_retriable_backend_error(_http_status_error(500)) is True

def test_retriable_unrelated_false():
    assert is_retriable_backend_error(ValueError("boom")) is False


def test_retriable_embedded_504_true():
    assert is_retriable_backend_error(EmbeddedChoiceError("x", status_code=504, raw_code=504)) is True

def test_retriable_embedded_auth_false():
    for code in (401, 403, 404):
        assert is_retriable_backend_error(EmbeddedChoiceError("x", status_code=code, raw_code=code)) is False

def test_retriable_embedded_nonnumeric_code_false():
    # insufficient_quota/invalid_api_key: код ПРИСУТСТВУЕТ, но нечисловой →
    # status_code=None, raw_code задан; повтором не чинится → не ретраим.
    assert is_retriable_backend_error(
        EmbeddedChoiceError("x", status_code=None, raw_code="insufficient_quota")
    ) is False

def test_retriable_embedded_no_code_true():
    # finish_reason='error' с пустым выбором: кода нет вовсе (raw_code=None) —
    # транзиентный сбой, ретрай оправдан.
    assert is_retriable_backend_error(EmbeddedChoiceError("x")) is True


def test_auth_error_http_401_true():
    assert is_auth_error(_http_status_error(401)) is True

def test_auth_error_stream_401_true():
    assert is_auth_error(BackendStreamError("unauthorized", status_code=401)) is True

def test_auth_error_403_and_500_false():
    for code in (403, 404, 500):
        assert is_auth_error(_http_status_error(code)) is False

def test_auth_error_unrelated_false():
    assert is_auth_error(ValueError("boom")) is False
    assert is_auth_error(BackendStreamError("x")) is False

def test_auth_error_embedded_401_true():
    # Прокси завернул 401 в HTTP-200 — тот же истёкший токен, лечится рефрешем.
    assert is_auth_error(EmbeddedChoiceError("x", status_code=401, raw_code=401)) is True

def test_auth_error_embedded_504_false():
    assert is_auth_error(EmbeddedChoiceError("x", status_code=504, raw_code=504)) is False


def test_finish_known_accept():
    for fr in ("stop", "tool_calls", "length"):
        d = normalize_finish_reason(fr, has_content=True, has_tool_calls=False)
        assert d.accept is True and d.unknown is False

def test_finish_unknown_with_content_accepts_flagged():
    d = normalize_finish_reason("content_filter", has_content=True, has_tool_calls=False)
    assert d.accept is True and d.unknown is True

def test_finish_null_with_tool_calls_accepts_flagged():
    d = normalize_finish_reason(None, has_content=False, has_tool_calls=True)
    assert d.accept is True and d.unknown is True

def test_finish_unknown_empty_rejects():
    d = normalize_finish_reason(None, has_content=False, has_tool_calls=False)
    assert d.accept is False and d.unknown is True
    d2 = normalize_finish_reason("eos", has_content=False, has_tool_calls=False)
    assert d2.accept is False
