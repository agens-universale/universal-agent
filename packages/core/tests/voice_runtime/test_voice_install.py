"""Оркестрация install на мок-runner: порядок шагов, ошибки, конфликт."""
import pytest

from gc_core.voice_runtime import install as inst
from gc_core.voice_runtime.state import (
    DEFAULT_MODEL, DEFAULT_PORT, SPEACHES_PIN, SPEACHES_PIN_SHA,
)


class R:
    def __init__(self, code=0):
        self.returncode = code
        self.stdout = ""
        self.stderr = ""


def _head(sha: str, code: int = 0) -> R:
    """Ответ `git rev-parse HEAD` мок-раннера."""
    result = R(code)
    result.stdout = sha + "\n"
    return result


def _is_rev_parse(cmd) -> bool:
    return "rev-parse" in " ".join(str(c) for c in cmd)


def _setup(monkeypatch, tmp_path, *, which=lambda name: f"/bin/{name}"):
    root = tmp_path / "speaches"
    monkeypatch.setattr("gc_core.voice_runtime.install.speaches_root", lambda: root)
    monkeypatch.setattr("gc_core.voice_runtime.install.shutil.which", which)
    saved = {}
    monkeypatch.setattr("gc_core.voice_runtime.install.save_state",
                        lambda st: saved.update(state=st))
    monkeypatch.setattr("gc_core.voice_runtime.install.wire_voice",
                        lambda port, model, language="ru":
                        saved.update(wired=(port, model, language)))
    monkeypatch.setattr("gc_core.voice_runtime.install.managed_language", lambda: "ru")
    monkeypatch.setattr("gc_core.voice_runtime.install._download_model",
                        lambda model: saved.update(downloaded=model))
    monkeypatch.setattr("gc_core.voice_runtime.install.validate_model_card", lambda model: None)
    return root, saved


def test_install_happy_path_order(monkeypatch, tmp_path):
    root, saved = _setup(monkeypatch, tmp_path)
    calls = []

    def runner(cmd, **kwargs):
        calls.append([str(c) for c in cmd])
        if calls and calls[-1][:2] == ["/bin/git", "clone"]:
            root.mkdir(parents=True, exist_ok=True)   # клон «создал» каталог
        return R(0)

    st = inst.install(runner=runner, echo=lambda *a: None)
    joined = [" ".join(c) for c in calls]
    assert any(c.startswith("/bin/git clone --depth 1 --branch " + SPEACHES_PIN)
               for c in joined)
    assert any("uv sync" in c and "--no-dev" in c for c in joined)
    assert any("pip" in c and "uninstall" in c and "onnxruntime-gpu" in c
               for c in joined)
    assert saved["wired"] == (DEFAULT_PORT, DEFAULT_MODEL, "ru")
    assert saved["state"].version == SPEACHES_PIN
    assert st.port == DEFAULT_PORT
    assert saved["downloaded"] == DEFAULT_MODEL


def test_install_requires_git_and_uv(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, which=lambda name: None)
    with pytest.raises(inst.VoiceInstallError, match="git"):
        inst.install(runner=lambda *a, **k: R(0), echo=lambda *a: None)


def test_install_fails_on_sync_error(monkeypatch, tmp_path):
    root, _ = _setup(monkeypatch, tmp_path)

    def runner(cmd, **kwargs):
        text = " ".join(str(c) for c in cmd)
        if "clone" in text:
            root.mkdir(parents=True, exist_ok=True)
            return R(0)
        if "sync" in text:
            return R(1)
        return R(0)

    with pytest.raises(inst.VoiceInstallError, match="uv sync"):
        inst.install(runner=runner, echo=lambda *a: None)


def test_install_warns_on_user_conflict(monkeypatch, tmp_path):
    root, _ = _setup(monkeypatch, tmp_path)
    root.mkdir(parents=True)          # «уже клонирован» (валидный) — clone пропускается
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    messages = []
    base = {"voice": {"url": "https://api.groq.com/x", "token": "gsk"}}
    inst.install(base_raw=base,
                 runner=lambda cmd, **k: _head(SPEACHES_PIN_SHA) if _is_rev_parse(cmd) else R(0),
                 echo=lambda *a: messages.append(" ".join(map(str, a))))
    assert any("перекро" in m and "groq" in m for m in messages)
    assert not any("клонирую" in m and "переклонирую" not in m for m in messages)


def test_install_keeps_language_chosen_earlier(monkeypatch, tmp_path):
    """Переустановка поверх существующего managed не имеет права молча вернуть язык
    к дефолту: его выбрал человек в /settings, install про это ничего не знает."""
    root, saved = _setup(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr("gc_core.voice_runtime.install.managed_language", lambda: "en")
    inst.install(runner=lambda cmd, **k: _head(SPEACHES_PIN_SHA) if _is_rev_parse(cmd) else R(0),
                 echo=lambda *a: None)
    assert saved["wired"][2] == "en"


def test_install_recovers_broken_clone(monkeypatch, tmp_path):
    """Огрызок каталога без pyproject (Windows-локи при remove) → переклонирование."""
    root, _ = _setup(monkeypatch, tmp_path)
    root.mkdir(parents=True)          # битый: без pyproject.toml
    calls = []

    def runner(cmd, **kwargs):
        calls.append(" ".join(str(c) for c in cmd))
        if "clone" in calls[-1]:
            root.mkdir(parents=True, exist_ok=True)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        return R(0)

    messages = []
    inst.install(runner=runner,
                 echo=lambda *a: messages.append(" ".join(map(str, a))))
    assert any("переклонирую" in m for m in messages)
    assert any("clone" in c for c in calls)


def _existing_clone(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")


def _clone_tracking_runner(root, head):
    """Мок git/uv: отвечает на rev-parse заданным HEAD, клон «создаёт» каталог."""
    calls = []

    def runner(cmd, **kwargs):
        calls.append(" ".join(str(c) for c in cmd))
        if _is_rev_parse(cmd):
            return head
        if "clone" in calls[-1]:
            _existing_clone(root)
        return R(0)

    return runner, calls


def test_install_reclones_when_pin_bumped(monkeypatch, tmp_path):
    """Бампнули SPEACHES_PIN: на диске лежит старая версия. Каталог есть — но брать его
    как есть нельзя, иначе state.json запишет новую версию поверх старого кода."""
    root, saved = _setup(monkeypatch, tmp_path)
    _existing_clone(root)
    runner, calls = _clone_tracking_runner(root, _head("0" * 40))   # чужой HEAD
    messages = []
    inst.install(runner=runner, echo=lambda *a: messages.append(" ".join(map(str, a))))
    assert any("не на " + SPEACHES_PIN in m and "переклонирую" in m for m in messages)
    assert any("clone --depth 1 --branch " + SPEACHES_PIN in c for c in calls)
    assert saved["state"].version == SPEACHES_PIN          # версия честная: клон свежий


def test_install_keeps_clone_already_at_pin(monkeypatch, tmp_path):
    """HEAD совпал с пином — переклонировать нечего, лишний трафик не тратим."""
    root, saved = _setup(monkeypatch, tmp_path)
    _existing_clone(root)
    runner, calls = _clone_tracking_runner(root, _head(SPEACHES_PIN_SHA))
    messages = []
    inst.install(runner=runner, echo=lambda *a: messages.append(" ".join(map(str, a))))
    assert any("уже на " + SPEACHES_PIN in m for m in messages)
    assert not any("clone --depth" in c for c in calls)
    assert saved["state"].version == SPEACHES_PIN


def test_version_check_survives_admin_owned_clone(monkeypatch, tmp_path):
    """Клон, поставленный из консоли администратора, принадлежит BUILTIN\\Администраторы,
    и git отказывается его читать («dubious ownership»). Поймано живой проверкой: без
    точечного safe.directory сверка не срабатывала никогда, каждый install уходил в
    переклон, а на занятом каталоге упал бы с «каталог занят»."""
    root, _ = _setup(monkeypatch, tmp_path)
    _existing_clone(root)
    seen = []

    def runner(cmd, **kwargs):
        text = [str(c) for c in cmd]
        if _is_rev_parse(cmd):
            seen.append(text)
            return _head(SPEACHES_PIN_SHA)
        return R(0)

    inst.install(runner=runner, echo=lambda *a: None)
    assert seen, "сверка версии не выполнялась вовсе"
    assert f"safe.directory={root}" in seen[0]


def test_install_reclones_when_version_unreadable(monkeypatch, tmp_path):
    """git не смог ответить (битый .git, чужой владелец каталога) — версия клона
    неизвестна. Объявить его годным значит вернуть ложь о версии: переклонируем."""
    root, _ = _setup(monkeypatch, tmp_path)
    _existing_clone(root)
    runner, calls = _clone_tracking_runner(root, _head("", code=128))
    messages = []
    inst.install(runner=runner, echo=lambda *a: messages.append(" ".join(map(str, a))))
    assert any("не прочитать" in m and "переклонирую" in m for m in messages)
    assert any("clone --depth" in c for c in calls)


def test_install_fails_when_broken_clone_locked(monkeypatch, tmp_path):
    """Битый каталог не удаляется (файлы заняты) → внятная ошибка, не uv sync."""
    root, _ = _setup(monkeypatch, tmp_path)
    root.mkdir(parents=True)          # битый и «залоченный»
    monkeypatch.setattr(inst, "force_rmtree", lambda p: False)
    with pytest.raises(inst.VoiceInstallError, match="занят"):
        inst.install(runner=lambda *a, **k: R(0), echo=lambda *a: None)


def test_install_rejects_model_without_ct2_card(monkeypatch, tmp_path):
    root, _ = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr("gc_core.voice_runtime.install.validate_model_card",
                        lambda model: "нет library_name: ctranslate2")
    def runner(cmd, **kwargs):
        root.mkdir(parents=True, exist_ok=True)
        (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        return R(0)
    with pytest.raises(inst.VoiceInstallError, match="не поддерживается"):
        inst.install(runner=runner, echo=lambda *a: None)


def test_install_card_network_error_warns_not_blocks(monkeypatch, tmp_path):
    """Сетевая ошибка проверки карточки — предупреждение, не блок (спека 6.2)."""
    real_validate_model_card = inst.validate_model_card  # _setup гасит на None — тут нужна настоящая
    root, saved = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr("gc_core.voice_runtime.install.validate_model_card", real_validate_model_card)
    monkeypatch.setattr("gc_core.voice_runtime.install._download_model",
                        lambda model: saved.update(downloaded=model))

    def bad_info(model):
        raise OSError("offline")
    monkeypatch.setattr("gc_core.voice_runtime.install._model_info", bad_info)
    messages = []
    def runner(cmd, **kwargs):
        root.mkdir(parents=True, exist_ok=True)
        (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        return R(0)
    inst.install(runner=runner, echo=lambda *a: messages.append(" ".join(map(str, a))))
    assert saved.get("downloaded")                    # установка продолжилась
    assert any("не удалось проверить" in m for m in messages)


def test_validate_model_card_criteria(monkeypatch):
    class Card:
        def __init__(self, lib, tags, pipeline=None):
            self.library_name = lib
            self.tags = tags
            self.pipeline_tag = pipeline
    class Info:
        def __init__(self, card):
            self.card_data = card
    ok = Info(Card("ctranslate2", ["automatic-speech-recognition"]))
    bad = Info(Card(None, []))
    monkeypatch.setattr("gc_core.voice_runtime.install._model_info", lambda m: ok)
    assert inst.validate_model_card("x") is None
    monkeypatch.setattr("gc_core.voice_runtime.install._model_info", lambda m: bad)
    assert "ctranslate2" in inst.validate_model_card("x")
