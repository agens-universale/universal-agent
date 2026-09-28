"""Шаг «Уровни моделей» плоского мастера: две необязательные под-формы."""

from gc_cli import onboarding_wizard as wz
from gc_core import onboarding
from gc_core.model_picker import ModelInfo


def _io(answers):
    it = iter(answers)
    out = []
    return wz.WizardIO(
        reader=lambda p, secret: next(it), printer=out.append
    ), out


def _models():
    return [
        ModelInfo("qwen/qwen3-8b", tool_capable=True),
        ModelInfo("qwen/qwen3-235b", tool_capable=True),
    ]


def test_question_is_asked_with_the_words_of_the_tui():
    io, out = _io([""])
    wz._collect_levels(io, {}, {}, _models())
    assert any("Добавить лёгкую и максимальную модели?" in m for m in out)


def test_declining_writes_nothing():
    values = {}
    io, _ = _io([""])
    wz._collect_levels(io, {}, values, _models())
    assert values == {}


def test_light_level_is_collected_under_its_own_key():
    values = {}
    io, _ = _io(["y", "1", ""])  # да · light = строка 1 · max — пропуск
    wz._collect_levels(io, {}, values, _models())
    assert values == {"model_light": "qwen/qwen3-8b"}


def test_skipping_one_level_leaves_the_other_alone():
    values = {}
    io, _ = _io(["y", "", "2"])  # light пропущен, max выбран
    wz._collect_levels(io, {}, values, _models())
    assert values == {"model_max": "qwen/qwen3-235b"}


def test_level_choice_is_confirmed_and_silent_on_skip():
    """То же правило, что и на шаге «Модель»: подтверждение — сразу после
    выбора, до записи в файл; пропущенный уровень остаётся немым — строка
    «сейчас» уже сказала, что там останется."""
    io, out = _io(["y", "1", ""])  # light = строка 1 · max — пропуск
    wz._collect_levels(io, {}, {}, _models())
    assert "  выбрано для light: qwen/qwen3-8b" in out
    assert not any(m == "  выбрано для max: qwen/qwen3-235b" for m in out)


def test_assigned_level_is_shown_as_current():
    cfg = {
        "models": {"fast": {"model": "qwen/qwen3-8b"}},
        "slots": {"light": "fast"},
    }
    io, out = _io(["y", "", ""])
    wz._collect_levels(io, cfg, {}, _models())
    assert any("сейчас: qwen/qwen3-8b  (имя записи «fast»)" in m for m in out)


def test_empty_level_says_it_is_unset():
    io, out = _io(["y", "", ""])
    wz._collect_levels(io, {}, {}, _models())
    assert any("сейчас: не задана" in m for m in out)


def test_without_a_list_levels_are_typed_freely():
    values = {}
    io, out = _io(["y", "vllm/Small", "vllm/Big"])
    wz._collect_levels(io, {}, values, None)
    assert values == {"model_light": "vllm/Small", "model_max": "vllm/Big"}
    assert any(wz._of.MODEL_NO_LIST in m for m in out)


def test_choice_is_confirmed_after_manual_entry_when_no_list():
    """Та же ветка без списка, но на уровнях: обе строки подтверждения
    должны появиться, а не только на шаге «Модель»."""
    io, out = _io(["y", "vllm/Small", "vllm/Big"])
    wz._collect_levels(io, {}, {}, None)
    assert "  выбрано для light: vllm/Small" in out
    assert "  выбрано для max: vllm/Big" in out


def test_the_model_list_is_fetched_once_for_both_steps(monkeypatch):
    """Список приходит с шага «Модель» и на уровнях не перезапрашивается: второй
    и третий поход в сеть за тем же ответом — задержка на медленном канале.

    Проверяется на `collect_fields`, а не на самом шаге: у `_collect_levels`
    список — параметр, и там ломаться нечему. Считаем походы в сеть за весь
    прогон мастера."""
    calls = []

    def fake(url, auth, **kw):
        calls.append(url)
        return _models()

    monkeypatch.setattr("gc_core.model_picker.fetch_models", fake)
    cfg = {
        "completions_url": "https://h/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer x"},
        "model": onboarding.MODEL_SENTINEL,
    }
    # адрес · авторизация · модель · уровни: да · light=1 · max — пропуск
    io, _ = _io(["", "", "", "y", "1", ""])
    values = wz.collect_fields(io, cfg, review_all=True)
    assert len(calls) == 1
    assert values == {"model_light": "qwen/qwen3-8b"}
