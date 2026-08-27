import requests

from utils.backend_errors import is_auth_error, is_context_overflow_error, is_empty_completion, is_retriable_backend_error, normalize_finish_reason
from utils.chat_completions_stream import BackendStreamError


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
    resp = requests.Response(); resp.status_code = 500
    assert is_retriable_backend_error(requests.exceptions.HTTPError(response=resp)) is True

def test_retriable_unrelated_false():
    assert is_retriable_backend_error(ValueError("boom")) is False


def test_auth_error_http_401_true():
    resp = requests.Response(); resp.status_code = 401
    assert is_auth_error(requests.exceptions.HTTPError(response=resp)) is True

def test_auth_error_stream_401_true():
    assert is_auth_error(BackendStreamError("unauthorized", status_code=401)) is True

def test_auth_error_403_and_500_false():
    for code in (403, 404, 500):
        resp = requests.Response(); resp.status_code = code
        assert is_auth_error(requests.exceptions.HTTPError(response=resp)) is False

def test_auth_error_unrelated_false():
    assert is_auth_error(ValueError("boom")) is False
    assert is_auth_error(BackendStreamError("x")) is False


def test_model_unavailable_covers_network_errors():
    from utils.backend_errors import is_model_unavailable_error
    assert is_model_unavailable_error(requests.exceptions.ConnectTimeout("timeout")) is True
    assert is_model_unavailable_error(requests.exceptions.SSLError("tls")) is True
    assert is_model_unavailable_error(requests.exceptions.ProxyError("proxy")) is True


def test_model_unavailable_covers_4xx_of_a_renamed_or_revoked_model():
    """§14 «404-переименование тем же путём»: провайдер снял или переименовал
    модель — это недоступность алиаса, а не ошибка запроса, и лечится она сменой
    уровня или профиля, как и 5xx. 401/403 здесь же: ключ отозвали именно у этой
    записи реестра (у каждой свой token), поэтому гаснет алиас, а не весь агент.

    Ветка `status_code in (401, 403, 404)` до этого теста не исполнялась ни разу:
    фоллбэк-сценарии гоняются на 500/503, а соседние тесты подают только сетевые
    и не-сетевые `RequestException` — её удаление не роняло ничего.
    """
    from utils.backend_errors import is_model_unavailable_error
    for code in (401, 403, 404):
        resp = requests.Response(); resp.status_code = code
        assert is_model_unavailable_error(
            requests.exceptions.HTTPError(f"{code}", response=resp)
        ) is True, f"HTTPError {code} не признан недоступностью модели"
        assert is_model_unavailable_error(
            BackendStreamError("model not found", status_code=code)
        ) is True, f"BackendStreamError {code} не признан недоступностью модели"

    # 400 — ошибка запроса, а не модели: смена уровня её не исправит.
    resp_400 = requests.Response(); resp_400.status_code = 400
    assert is_model_unavailable_error(
        requests.exceptions.HTTPError("400", response=resp_400)
    ) is False


def test_model_unavailable_ignores_non_network_request_exceptions():
    """Предохранитель гасит алиас на ВСЮ сессию, поэтому «недоступность» — только
    сетевые ошибки и 5xx/401/403/404. Битое тело ответа живого провайдера и кривой
    url/прокси — тоже `RequestException`, но моделью не лечатся: смена уровня или
    профиля их не исправит, а алиас остался бы закрытым до правки конфига."""
    from utils.backend_errors import is_model_unavailable_error
    assert is_model_unavailable_error(requests.exceptions.MissingSchema("no scheme")) is False
    assert is_model_unavailable_error(requests.exceptions.InvalidURL("bad url")) is False
    assert is_model_unavailable_error(requests.exceptions.TooManyRedirects("loop")) is False
    assert is_model_unavailable_error(
        requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
    ) is False


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
