"""upgrade_gate: стамп agent_state.json + детект «меня обновили»."""
from gc_core import app_version
from gc_core import upgrade_gate


def _fake_versions(monkeypatch, version="0.2.0", commit="abc"):
    monkeypatch.setattr(app_version, "agent_version", lambda: version)
    monkeypatch.setattr(
        app_version, "install_source",
        lambda _read=None: app_version.InstallSource(
            url="u", ref=None, commit=commit, editable=False),
    )


def test_no_stamp_is_not_an_upgrade(monkeypatch, tmp_path):
    # Первый запуск на машине: стампа нет → НЕ апгрейд (иначе ложное «обновлён»)
    _fake_versions(monkeypatch)
    p = tmp_path / "agent_state.json"
    assert upgrade_gate.detect_upgrade(path=p) is None


def test_same_version_and_commit_is_not_an_upgrade(monkeypatch, tmp_path):
    _fake_versions(monkeypatch)
    p = tmp_path / "agent_state.json"
    upgrade_gate.mark_current_run(path=p)
    assert upgrade_gate.detect_upgrade(path=p) is None


def test_version_change_detected(monkeypatch, tmp_path):
    p = tmp_path / "agent_state.json"
    _fake_versions(monkeypatch, version="0.2.0")
    upgrade_gate.mark_current_run(path=p)
    _fake_versions(monkeypatch, version="0.3.0")
    info = upgrade_gate.detect_upgrade(path=p)
    assert info == upgrade_gate.UpgradeInfo(old_version="0.2.0", new_version="0.3.0")


def test_commit_change_detected_even_without_version_bump(monkeypatch, tmp_path):
    # страховка от забытого бампа pyproject: релиз без бампа всё равно ловится
    p = tmp_path / "agent_state.json"
    _fake_versions(monkeypatch, commit="abc")
    upgrade_gate.mark_current_run(path=p)
    _fake_versions(monkeypatch, commit="def")
    assert upgrade_gate.detect_upgrade(path=p) is not None


def test_broken_state_file_treated_as_empty(monkeypatch, tmp_path):
    _fake_versions(monkeypatch)
    p = tmp_path / "agent_state.json"
    p.write_text("не json{", encoding="utf-8")
    assert upgrade_gate.read_state(path=p) == {}
    assert upgrade_gate.detect_upgrade(path=p) is None


def test_write_state_merges_fields(monkeypatch, tmp_path):
    _fake_versions(monkeypatch)
    p = tmp_path / "agent_state.json"
    upgrade_gate.write_state({"last_update_check_ts": 123}, path=p)
    upgrade_gate.mark_current_run(path=p)
    state = upgrade_gate.read_state(path=p)
    assert state["last_update_check_ts"] == 123
    assert state["last_run_version"] == "0.2.0"
