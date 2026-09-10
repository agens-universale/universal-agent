import requests

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


def test_empty_completion_true_when_nothing():
    assert is_empty_completion(_resp()) is True

def test_empty_completion_no_choices_is_empty():
    assert is_empty_completion({"choices": []}) is True
    assert is_empty_completion({}) is True

def test_not_empty_with_content():
    assert is_empty_completion(_resp(content="hi")) is False

def test_not_empty_with_tool_calls():
    assert is_empty_completion(_resp(tool_calls=[{"id": "c1", "type": "function"}])) is False


def test_not_empty_with_reasoning_content():
    dct = _resp()
    dct["choices"][0]["message"]["reasoning_content"] = "search the leaderboard"
    assert is_empty_completion(dct) is False


def test_empty_whitespace_reasoning_without_finish_is_empty():
    dct = _resp()
    dct["choices"][0]["message"]["reasoning_content"] = "  \n"
    assert is_empty_completion(dct) is True

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

def test_auth_error_embedded_401_true():
    # Прокси завернул 401 в HTTP-200 — тот же истёкший токен, лечится рефрешем.
    assert is_auth_error(EmbeddedChoiceError("x", status_code=401, raw_code=401)) is True

def test_auth_error_embedded_504_false():
    assert is_auth_error(EmbeddedChoiceError("x", status_code=504, raw_code=504)) is False


def test_model_unavailable_covers_network_errors():
    from gc_core.utils.backend_errors import is_model_unavailable_error
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
    from gc_core.utils.backend_errors import is_model_unavailable_error
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
    from gc_core.utils.backend_errors import is_model_unavailable_error
    assert is_model_unavailable_error(requests.exceptions.MissingSchema("no scheme")) is False
    assert is_model_unavailable_error(requests.exceptions.InvalidURL("bad url")) is False
    assert is_model_unavailable_error(requests.exceptions.TooManyRedirects("loop")) is False
    assert is_model_unavailable_error(
        requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
    ) is False


def _embedded_exc(response):
    """Исключение строит ПРОД (`_raise_on_embedded_choice_error`) из реального
    тела ответа — руками не собираем: иначе тест проверял бы наши догадки о
    форме, а не то, что действительно присылает прокси."""
    from gc_core.request_backend_policy import _raise_on_embedded_choice_error
    try:
        _raise_on_embedded_choice_error(response)
    except EmbeddedChoiceError as exc:
        return exc
    raise AssertionError("ожидали EmbeddedChoiceError")


def test_model_unavailable_covers_embedded_choice_errors():
    """Прокси заворачивает отказ в HTTP-200 (choices[0].error), поэтому такой же
    отказ модели приходит другим типом исключения. Без этой ветки предохранитель
    и фоллбэк «привязка недоступна → активная модель» слепы ровно к тем
    провайдерам, ради которых develop и завёл EmbeddedChoiceError."""
    from gc_core.utils.backend_errors import is_model_unavailable_error
    upstream_timeout = {"choices": [{"error": {
        "code": 504, "message": "Upstream idle timeout",
        "metadata": {"error_type": "timeout"}}}]}
    assert is_model_unavailable_error(_embedded_exc(upstream_timeout)) is True
    assert is_model_unavailable_error(
        _embedded_exc({"choices": [{"error": {"code": 401, "message": "bad key"}}]})
    ) is True
    assert is_model_unavailable_error(
        _embedded_exc({"choices": [{"error": {"code": 404, "message": "no such model"}}]})
    ) is True
    # Вторая типовая форма — ошибка верхнего уровня, без choices.
    assert is_model_unavailable_error(
        _embedded_exc({"error": {"code": 503, "message": "service unavailable"}})
    ) is True


def test_model_unavailable_ignores_embedded_without_http_status():
    """Нечисловой код (insufficient_quota/invalid_api_key) и полное отсутствие
    кода (finish_reason="error" с пустым выбором) дают status_code=None. Гасить
    по ним алиас на всю сессию нельзя: первое — деньги и ключи, повтором другой
    моделью не лечится, второе — неизвестно что. Та же осторожность, что у
    BackendStreamError без статуса."""
    from gc_core.utils.backend_errors import is_model_unavailable_error
    assert is_model_unavailable_error(
        _embedded_exc({"choices": [{"error": {
            "code": "insufficient_quota", "message": "quota exceeded"}}]})
    ) is False
    assert is_model_unavailable_error(
        _embedded_exc({"choices": [{"finish_reason": "error", "message": {}}]})
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


def test_finish_null_with_reasoning_accepts_flagged():
    d = normalize_finish_reason(
        None, has_content=False, has_tool_calls=False, has_reasoning=True
    )
    assert d.accept is True and d.unknown is True
