from tools.skills.registry import SkillsRegistry


def _write_skill(root, sid):
    d = root / sid
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {sid}\ndescription: d\n---\nbody\n", encoding="utf-8"
    )


def _isolated(system):
    """Реестр только с тестовым system_root — без user/project скиллов среды."""
    return SkillsRegistry(system_root=str(system), user_root="", project_root="")


def test_get_skill_self_heals_for_just_created(tmp_path):
    system = tmp_path / "system"
    _write_skill(system, "first")
    reg = _isolated(system)
    reg.list_skills()                         # загрузка кэша (видит только first)
    _write_skill(system, "alias-game")        # скилл создан «посреди прогона»
    detail = reg.load_skill_detail("alias-game")   # промах → reload → найден
    assert detail["id"] == "alias-game"


def test_get_skill_still_raises_for_missing(tmp_path):
    system = tmp_path / "system"
    _write_skill(system, "first")
    reg = _isolated(system)
    reg.list_skills()
    import pytest
    with pytest.raises(KeyError):
        reg.load_skill_detail("does-not-exist")


def test_list_skills_is_fresh(tmp_path):
    system = tmp_path / "system"
    _write_skill(system, "first")
    reg = _isolated(system)
    ids0 = {s["id"] for s in reg.list_skills()}
    assert ids0 == {"first"}
    _write_skill(system, "second")
    ids1 = {s["id"] for s in reg.list_skills()}   # свежий список без рестарта
    assert "second" in ids1


def test_reference_slug_miss_does_not_reload(tmp_path, monkeypatch):
    """Промах slug reference у существующего скилла НЕ триггерит reload."""
    system = tmp_path / "system"
    skill = system / "qa"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: qa\ndescription: d\n---\nb\n", encoding="utf-8")
    (skill / "references" / "good.md").write_text("g", encoding="utf-8")
    reg = _isolated(system)
    reg.load_skill_detail("qa")               # загрузка кэша (1 скан)
    calls = {"n": 0}
    orig = reg._load_all_skills
    def _counting():
        calls["n"] += 1
        orig()
    monkeypatch.setattr(reg, "_load_all_skills", _counting)
    import pytest
    with pytest.raises(KeyError):
        reg.load_reference("qa", "no-such-slug")   # скилл найден, slug — нет
    assert calls["n"] == 0                     # reload не вызывался (скилл был найден)
