"""probe_line_text: рендер строки статуса пробы — чистые юниты без пилота."""
import pytest

pytest.importorskip("textual")

from model_picker import ModelInfo
from tui.services.probe_presentation import probe_line_text
from tui.services.settings_probe import ProbeResult


def _ok(models, detail="доступен", latency_ms=42):
    return ProbeResult(verdict="ok", http_status=200, models=models,
                       latency_ms=latency_ms, detail=detail)


def test_none_result_is_not_checked():
    assert probe_line_text(None).plain == "— не проверено"


def test_ok_without_models_no_tail():
    r = _ok(None)
    assert probe_line_text(r).plain == "✓ доступен · 42 ms"


def test_ok_with_models_count_no_model_id():
    r = _ok([ModelInfo(id="a"), ModelInfo(id="b")])
    assert probe_line_text(r).plain == "✓ доступен · 42 ms · 2 модели на сервере"


def test_ok_model_id_found_appends_tail():
    r = _ok([ModelInfo(id="glm-5"), ModelInfo(id="other")])
    text = probe_line_text(r, model_id="glm-5")
    assert text.plain == "✓ доступен · 42 ms · 2 модели на сервере · glm-5 найдена на сервере"


def test_ok_model_id_not_found_replaces_whole_line_with_warning():
    r = _ok([ModelInfo(id="other")])
    text = probe_line_text(r, model_id="glm-5")
    assert text.plain == "⚠ модель glm-5 на сервере не найдена"


def test_ok_model_id_given_but_models_none_has_no_tail():
    r = _ok(None)
    text = probe_line_text(r, model_id="glm-5")
    assert text.plain == "✓ доступен · 42 ms"


def test_rejected():
    r = ProbeResult(verdict="rejected", http_status=401, models=None,
                    latency_ms=5, detail="токен отклонён (401)")
    assert probe_line_text(r).plain == "✗ токен отклонён (401)"


def test_unknown():
    r = ProbeResult(verdict="unknown", http_status=None, models=None,
                    latency_ms=0, detail="нет соединения (timeout)")
    assert probe_line_text(r).plain == "⚠ не удалось проверить: нет соединения (timeout)"
