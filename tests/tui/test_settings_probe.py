"""settings_probe: маппинг двухступенчатого probe в три вердикта."""
import pytest

pytest.importorskip("textual")

import model_picker as mp
from tui.services.settings_probe import ProbeResult, interpret_auth, probe


def _fetch(models=None, status=None, net="", latency=7):
    def fetch_ex(endpoint, auth, *, verify=True, http_get_ex=None):
        return mp.FetchOutcome(models, status, net, latency)
    return fetch_ex


def _post(status=None, net=""):
    def post_ex(endpoint, auth, model, *, verify=True, http_post_ex=None):
        return status, net, 11
    return post_ex


def test_ok_from_stage1_models():
    r = probe("http://x/v1/chat/completions", None, True, "m1",
              fetch_ex=_fetch(models=[mp.ModelInfo(id="m1")], status=200))
    assert r.verdict == "ok" and r.models and r.latency_ms == 7
    assert r.reachable and r.auth_ok


def test_rejected_401_from_stage1():
    r = probe("http://x/v1/chat/completions", "Bearer bad", True, "m1",
              fetch_ex=_fetch(status=401))
    assert r.verdict == "rejected" and r.http_status == 401
    assert "401" in r.detail and not r.auth_ok


def test_network_stage1_is_unknown():
    r = probe("http://x/v1/chat/completions", None, True, "m1",
              fetch_ex=_fetch(net="timeout"))
    assert r.verdict == "unknown" and not r.reachable
    assert "timeout" in r.detail


def test_404_on_models_falls_to_stage2_ok():
    r = probe("http://x/v1/chat/completions", None, True, "m1",
              fetch_ex=_fetch(status=404), post_ex=_post(status=200))
    assert r.verdict == "ok" and r.models is None   # сервер жив, списка нет


def test_404_on_models_and_stage2_404_is_rejected():
    r = probe("http://x/v1/chat/completions", None, True, "m1",
              fetch_ex=_fetch(status=404), post_ex=_post(status=404))
    assert r.verdict == "rejected" and r.http_status == 404
    assert "404" in r.detail


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


def test_no_model_for_stage2_is_unknown():
    r = probe("http://x/v1/chat/completions", None, True, "",
              fetch_ex=_fetch(status=404))
    assert r.verdict == "unknown"
    assert "модел" in r.detail                      # «задайте модель»


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
