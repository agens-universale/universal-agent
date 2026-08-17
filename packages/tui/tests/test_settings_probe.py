"""settings_probe: маппинг двухступенчатого probe в три вердикта."""
import pytest

pytest.importorskip("textual")

from gc_core import model_picker as mp
from gc_tui.services.settings_probe import ProbeResult, interpret_auth, probe


def _fetch(models=None, status=None, net="", latency=7):
    def fetch_ex(endpoint, auth, *, verify=True, http_get_ex=None):
        return mp.FetchOutcome(models, status, net, latency)
    return fetch_ex


def _post(status=None, net="", json_body=True):
    def post_ex(endpoint, auth, model, *, verify=True, http_post_ex=None):
        return status, net, 11, json_body
    return post_ex


def test_ok_from_stage1_models():
    r = probe("http://x/v1/chat/completions", None, True, "m1",
              fetch_ex=_fetch(models=[mp.ModelInfo(id="m1")], status=200))
    assert r.verdict == "ok" and r.models and r.latency_ms == 7
    assert r.reachable and r.auth_ok


def test_stage1_401_with_working_post_is_ok():
    """gigacode ST: /config со своей plugin-авторизацией даёт 401 при валидном
    Bearer, /models отсутствует (404) → цепочка схлопывается в 401. Вердикт о
    токене — по реальному маршруту: POST в chat/completions прошёл → ok."""
    r = probe("http://x/v1/chat/completions", "Bearer good", True, "m1",
              fetch_ex=_fetch(status=401), post_ex=_post(status=200))
    assert r.verdict == "ok" and r.auth_ok


def test_stage1_401_and_post_401_is_rejected():
    """401 и на models-цепочке, и на самом chat/completions → токен отклонён."""
    r = probe("http://x/v1/chat/completions", "Bearer bad", True, "m1",
              fetch_ex=_fetch(status=401), post_ex=_post(status=401))
    assert r.verdict == "rejected" and r.http_status == 401
    assert "401" in r.detail and not r.auth_ok


def test_no_model_dummy_post_non401_token_accepted():
    """Первичная настройка (адрес+токен, модели ещё нет): 401 цепочки + POST
    с заглушкой ответил 500 «незнакомая модель» → авторизация пройдена → ok.
    Строка «Подключение» судит только сервер+токен, модель не её забота."""
    r = probe("http://x/v1/chat/completions", "Bearer good", True, "",
              fetch_ex=_fetch(status=401), post_ex=_post(status=500))
    assert r.verdict == "ok" and r.auth_ok


def test_no_model_dummy_post_401_is_rejected():
    """Без модели: POST-заглушка тоже 401 → токен отклонён."""
    r = probe("http://x/v1/chat/completions", "Bearer bad", True, "",
              fetch_ex=_fetch(status=401), post_ex=_post(status=401))
    assert r.verdict == "rejected" and r.http_status == 401


def test_no_model_dummy_post_404_json_is_ok():
    """404 с JSON-телом = ответило приложение («model not found» на заглушку) —
    до валидации тела нельзя дойти, не пройдя авторизацию → токен принят."""
    r = probe("http://x/v1/chat/completions", "Bearer good", True, "",
              fetch_ex=_fetch(status=401), post_ex=_post(status=404, json_body=True))
    assert r.verdict == "ok" and r.auth_ok


def test_no_model_dummy_post_404_html_is_unknown():
    """404 без JSON на заглушку — неоднозначно: gigacode ST так отвечает и на
    НЕЗНАКОМУЮ МОДЕЛЬ (роутер, не Express-404 пути) → не заявляем «путь не
    найден» и не виним токен; честное «не удалось проверить». Differential
    спасает случай через 401→404 (см. interpret_auth-тесты)."""
    r = probe("http://x/v1/chat/completions", "Bearer good", True, "",
              fetch_ex=_fetch(status=401), post_ex=_post(status=404, json_body=False))
    assert r.verdict == "unknown" and r.http_status == 404
    assert r.auth_ok


def test_no_model_sends_dummy_probe_model():
    """Без модели ступень 2 шлёт инертную заглушку, а не пустую строку."""
    seen = {}

    def post_ex(endpoint, auth, model, *, verify=True, http_post_ex=None):
        seen["model"] = model
        return 500, "", 11, True

    r = probe("http://x/v1/chat/completions", "Bearer good", True, "",
              fetch_ex=_fetch(status=401), post_ex=post_ex)
    assert r.verdict == "ok"
    assert seen["model"] and "probe" in seen["model"]


def test_network_stage1_is_unknown():
    r = probe("http://x/v1/chat/completions", None, True, "m1",
              fetch_ex=_fetch(net="timeout"))
    assert r.verdict == "unknown" and not r.reachable
    assert "timeout" in r.detail


def test_404_on_models_falls_to_stage2_ok():
    r = probe("http://x/v1/chat/completions", None, True, "m1",
              fetch_ex=_fetch(status=404), post_ex=_post(status=200))
    assert r.verdict == "ok" and r.models is None   # сервер жив, списка нет


def test_404_on_models_and_stage2_404_json_blames_model():
    """Реальная модель, 404 с JSON-телом → приложение не знает модель."""
    r = probe("http://x/v1/chat/completions", None, True, "m1",
              fetch_ex=_fetch(status=404), post_ex=_post(status=404))
    assert r.verdict == "rejected" and r.http_status == 404
    assert "404" in r.detail and "модел" in r.detail


def test_404_on_models_and_stage2_404_html_names_both():
    """Реальная модель, 404 без JSON: не различить «кривой путь» от «роутер не
    знает модель» (gigacode ST на несуществующую модель отвечает 404-не-JSON,
    как и на кривой путь) → честное объединённое «адрес и имя модели»."""
    r = probe("http://x/v1/chat/completions", None, True, "m1",
              fetch_ex=_fetch(status=404), post_ex=_post(status=404, json_body=False))
    assert r.verdict == "rejected" and r.http_status == 404
    assert "адрес" in r.detail and "модел" in r.detail


def test_stage2_401_is_rejected():
    r = probe("http://x/v1/chat/completions", "Bearer bad", True, "m1",
              fetch_ex=_fetch(status=404), post_ex=_post(status=401))
    assert r.verdict == "rejected" and r.http_status == 401


def test_stage2_network_is_unknown():
    r = probe("http://x/v1/chat/completions", None, True, "m1",
              fetch_ex=_fetch(status=404), post_ex=_post(net="conn refused"))
    assert r.verdict == "unknown"


def test_stage2_5xx_is_unknown_not_rejected():
    r = probe("http://x/v1/chat/completions", None, True, "m1",
              fetch_ex=_fetch(status=404), post_ex=_post(status=500))
    assert r.verdict == "unknown"                   # 500 — не вердикт о данных


def test_no_model_stage1_404_dummy_post_ok():
    """Без модели ступень 2 работает и после 404-цепочки (заглушка вместо
    «задайте модель»): POST прошёл → сервер жив, auth ок."""
    r = probe("http://x/v1/chat/completions", None, True, "",
              fetch_ex=_fetch(status=404), post_ex=_post(status=200))
    assert r.verdict == "ok"


# --- interpret_auth: differential-probe авторизации ---
def _ok():
    return ProbeResult("ok", 200, None, 42, "доступен")


def _r401():
    return ProbeResult("rejected", 401, None, 30, "токен отклонён (401)")


def test_auth_no_token_required_when_control_ok():
    """Контроль без токена прошёл → авторизация не требуется (не «токен принят»)."""
    r = interpret_auth(_ok(), None, has_token=False)
    assert r.verdict == "ok"
    assert "не требует" in r.detail or "без токена" in r.detail
    assert "токен принят" not in r.detail


def test_auth_typed_token_not_checked_when_control_ok():
    """Введён токен, но сервер отдаёт и без него → токен не проверяется."""
    r = interpret_auth(_ok(), None, has_token=True)
    assert r.verdict == "ok" and "не проверяется" in r.detail


def test_auth_required_but_no_token():
    """Контроль без токена → 401, токена нет → сервер требует авторизацию."""
    r = interpret_auth(_r401(), None, has_token=False)
    assert r.verdict == "rejected" and "требует авторизацию" in r.detail


def test_auth_token_accepted():
    """Сервер требует авторизацию (401 без токена), с токеном → ok → «токен принят»."""
    r = interpret_auth(_r401(), _ok(), has_token=True)
    assert r.verdict == "ok" and r.detail == "токен принят"


def test_auth_token_rejected():
    """Сервер требует авторизацию, с токеном тоже 401 → токен отклонён."""
    r = interpret_auth(_r401(), _r401(), has_token=True)
    assert r.verdict == "rejected" and "отклонён" in r.detail


def test_auth_accepted_when_401_moves_to_404():
    """Живой кейс ST (несуществующая модель): контроль без токена → 401, с
    токеном → 404. Отказ «переехал» дальше авторизации → путь существует,
    ТОКЕН ПРИНЯТ; 404 — про модель, это забота секции «Модель»."""
    wt = ProbeResult("rejected", 404, None, 25,
                     "отклонено сервером (404): проверьте адрес и имя модели")
    r = interpret_auth(_r401(), wt, has_token=True)
    assert r.verdict == "ok" and r.auth_state == "accepted"
    assert r.detail == "токен принят"


def test_auth_accepted_when_401_moves_to_5xx():
    """Тот же принцип для 5xx: с токеном сервер упал дальше авторизации."""
    wt = ProbeResult("unknown", 500, None, 25, "сервер ответил 500")
    r = interpret_auth(_r401(), wt, has_token=True)
    assert r.verdict == "ok" and r.auth_state == "accepted"


def test_auth_not_claimed_when_with_token_network_fails():
    """С токеном сеть упала (status None) → про токен ничего не утверждаем."""
    wt = ProbeResult("unknown", None, None, 5000, "нет соединения (timeout)")
    r = interpret_auth(_r401(), wt, has_token=True)
    assert r.auth_state != "accepted" and r.verdict == "unknown"


def test_auth_unknown_control_passes_through():
    """Сеть/VPN на контроле → unknown, авторизацию не наказываем."""
    ctrl = ProbeResult("unknown", None, None, 5000, "нет соединения (timeout)")
    r = interpret_auth(ctrl, None, has_token=True)
    assert r.verdict == "unknown" and r is ctrl


def test_auth_404_control_not_about_auth():
    """404 на контроле — проблема пути/модели, не авторизации → отдаём как есть."""
    ctrl = ProbeResult("rejected", 404, None, 20, "отклонено сервером (404): проверьте путь")
    r = interpret_auth(ctrl, None, has_token=False)
    assert r.verdict == "rejected" and "404" in r.detail and "авторизаци" not in r.detail
