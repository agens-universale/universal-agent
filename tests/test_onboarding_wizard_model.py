import onboarding
import onboarding_wizard as wz
from model_picker import ModelInfo


def _io(answers):
    it = iter(answers)
    out = []
    return wz.WizardIO(reader=lambda p, secret: next(it), printer=out.append), out


def test_model_pick_from_list():
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"},
           "model": onboarding.MODEL_SENTINEL}
    fake = lambda url, auth, **k: [ModelInfo("Qwen", tool_capable=True), ModelInfo("CodeChat", tool_capable=False)]
    io, _ = _io(["1"])   # выбрать пункт 1 (Qwen)
    values = {}
    wz._collect_model(io, cfg, values, fetch=fake)
    assert values["model"] == "Qwen"


def test_model_manual_fallback_when_no_list():
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"},
           "model": onboarding.MODEL_SENTINEL}
    fake = lambda url, auth, **k: None   # список не получен
    io, _ = _io(["vllm/Qwen3-Coder-Next-262k"])   # ручной ввод
    values = {}
    wz._collect_model(io, cfg, values, fetch=fake)
    assert values["model"] == "vllm/Qwen3-Coder-Next-262k"


def test_model_warning_shown_only_with_non_tool():
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"}, "model": onboarding.MODEL_SENTINEL}
    io, out = _io(["1"])
    wz._collect_model(io, cfg, {}, fetch=lambda *a, **k: [ModelInfo("Q", tool_capable=True)])
    assert not any("⚠" in m for m in out)   # все tool-capable → без ⚠


def test_model_manual_fallback_when_empty_list():
    """fetch_models -> [] (пустой список) → ручной ввод, не пустое меню."""
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"},
           "model": onboarding.MODEL_SENTINEL}
    fake = lambda url, auth, **k: []   # пустой список
    io, _ = _io(["vllm/SomeModel"])
    values = {}
    wz._collect_model(io, cfg, values, fetch=fake)
    assert values["model"] == "vllm/SomeModel"


def test_model_warning_shown_when_non_tool_present():
    """⚠-подсказка появляется, если хотя бы одна модель tool_capable is False."""
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"}, "model": onboarding.MODEL_SENTINEL}
    io, out = _io(["1"])
    wz._collect_model(io, cfg, {}, fetch=lambda *a, **k: [
        ModelInfo("Q", tool_capable=True), ModelInfo("C", tool_capable=False)
    ])
    assert any("⚠ отмеченные модели" in m for m in out)


def test_model_manual_entry_option():
    """Выбор последнего пункта 'вписать вручную' → ручной ввод."""
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"}, "model": onboarding.MODEL_SENTINEL}
    io, _ = _io(["3", "my-custom-model"])   # 2 модели → пункт 3 = вручную
    values = {}
    wz._collect_model(io, cfg, values, fetch=lambda *a, **k: [
        ModelInfo("A", tool_capable=True), ModelInfo("B", tool_capable=True)
    ])
    assert values["model"] == "my-custom-model"


def test_model_section_header_and_marker_consistent_when_no_list():
    # единый плоский стиль: заголовок «  Модель» даже без списка, приглашение «  > », без labeled-текста
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"}, "model": onboarding.MODEL_SENTINEL}
    prompts, out = [], []
    io = wz.WizardIO(reader=lambda p, s: prompts.append(p) or "", printer=out.append)
    wz._collect_model(io, cfg, {}, fetch=lambda *a, **k: None)   # список не получен
    assert any(l.strip() == "Модель" for l in out)       # заголовок секции есть всегда
    assert "  > " in prompts                             # приглашение в общем стиле
    assert not any("Имя модели" in p for p in prompts)   # старое labeled-приглашение убрано


def test_model_manual_prompt_marker_from_list():
    # ручной ввод после выбора «вписать вручную» — тоже «  > »
    cfg = {"completions_url": "https://h/api/v2/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer x"}, "model": onboarding.MODEL_SENTINEL}
    prompts = []
    seq = iter(["3", "my-model"])
    io = wz.WizardIO(reader=lambda p, s: (prompts.append(p), next(seq))[1], printer=lambda m: None)
    values = {}
    wz._collect_model(io, cfg, values, fetch=lambda *a, **k: [
        ModelInfo("A", tool_capable=True), ModelInfo("B", tool_capable=True)])
    assert values["model"] == "my-model"
    assert "  > " in prompts
    assert not any("Имя модели" in p for p in prompts)
