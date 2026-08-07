"""Пресет-лист у поля «URL» облачной голосовой панели (спека 2026-08-05 §3–§4)."""
import pytest

pytest.importorskip("textual")

from textual.app import App, ComposeResult

from config_modules.endpoint_presets import EndpointPreset

PRESETS = [
    EndpointPreset(name="OpenAI", url="https://api.openai.com/v1/audio/transcriptions"),
    EndpointPreset(name="Groq", url="https://api.groq.com/openai/v1/audio/transcriptions"),
]


def _harness(editor):
    class _App(App):
        def compose(self) -> ComposeResult:
            yield editor
    return _App()


def _editor(values=None, *, url_presets=None, readonly=(),
           fields=("url", "token", "model")):
    from tui.editors.voice_endpoint import VoiceEndpointEditor
    return VoiceEndpointEditor(values=values or {}, fields=fields, autofocus=False,
                               readonly=readonly, url_presets=url_presets)


async def test_url_list_exists_only_when_presets_given():
    ed = _editor(url_presets=list(PRESETS))
    app = _harness(ed)
    async with app.run_test():
        assert ed.url_list is not None and ed.list_for("url") is ed.url_list

    ed2 = _editor(url_presets=None)
    app2 = _harness(ed2)
    async with app2.run_test():
        assert ed2.url_list is None and ed2.list_for("url") is None


async def test_readonly_url_gets_no_list():
    ed = _editor(url_presets=list(PRESETS), readonly=("url", "token", "model"))
    app = _harness(ed)
    async with app.run_test():
        assert ed.url_list is None


async def test_pick_of_foreign_preset_writes_url_and_clears_model():
    """В поле URL — адрес OpenAI, в модели — whisper-1; выбор Groq → URL сменился,
    модель пуста. Подстановка идёт РЕАЛЬНЫМ путём (open → move → pick_in), а не
    прямым вызовом колбэка."""
    ed = _editor({"url": PRESETS[0].url, "model": "whisper-1"},
                url_presets=list(PRESETS))
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.url_list.open()
        assert ed.url_list.move(1) is True          # OpenAI -> Groq
        assert ed.pick_in("url") is True
        await pilot.pause()
        assert ed.values()["url"] == PRESETS[1].url
        assert ed.values()["model"] == ""


async def test_pick_of_same_preset_keeps_model():
    """В поле URL уже адрес OpenAI; выбор того же OpenAI → модель НЕ тронута."""
    ed = _editor({"url": PRESETS[0].url, "model": "whisper-1"},
                url_presets=list(PRESETS))
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.url_list.open()                          # входит с подсветкой ЭТОГО пресета
        assert ed.pick_in("url") is True
        await pilot.pause()
        assert ed.values()["url"] == PRESETS[0].url
        assert ed.values()["model"] == "whisper-1"


async def test_submit_with_empty_model_is_a_form_error():
    """url валиден, модель пуста → (False, None) и строка ошибки «выберите модель»."""
    ed = _editor({"url": "https://s.x/v1/audio/transcriptions", "model": ""})
    app = _harness(ed)
    async with app.run_test():
        ok, values = ed.submit()
        assert (ok, values) == (False, None)
        assert any("выберите модель" in str(w.render()) for w in ed.query(".onb-warn"))


def test_stt_markers_include_voxtral():
    from tui.editors.voice_endpoint import _STT_MARKERS, stt_models_first
    assert "voxtral" in _STT_MARKERS
    assert stt_models_first(["voxtral-small-latest", "gpt-x"]) == ["voxtral-small-latest"]
