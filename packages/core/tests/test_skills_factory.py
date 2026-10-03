from pathlib import Path

import pytest
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars
from gc_core.tools.skills.registry import get_skills_registry
from gc_testkit.settings import make_settings


def _write_skill(root: Path, skill_id: str, description: str) -> None:
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f'---\nid: {skill_id}\nname: {skill_id}\ndescription: "{description}"\n---\n# body\n',
        encoding="utf-8",
    )


@pytest.fixture
def isolated_env_empty_layers(monkeypatch, tmp_path):
    """User и project пусты; system берётся из реального <repo>/skills."""
    fake = EnvVars(gc_home=tmp_path / "home")
    monkeypatch.setattr(env_vars, "env", fake)
    monkeypatch.chdir(tmp_path)  # project = tmp_path/.gc/skills (нет)
    return fake


async def test_factory_three_layer_when_no_path(
    isolated_env_empty_layers, tmp_path
):
    """Без skills_data_path в config → three-layer registry, видит system-скиллы."""
    reg = get_skills_registry(make_settings({}))
    items = await reg.list_skills()
    # System-слой реальный (<repo>/skills/) — содержит как минимум один встроенный скилл.
    assert len(items) >= 1
    # Все из system, потому что user/project в этом окружении пусты.
    assert all(s["source"] == "system" for s in items)


async def test_factory_three_layer_when_config_is_none(
    isolated_env_empty_layers,
):
    """settings=None работает так же, как пустые настройки."""
    reg = get_skills_registry(None)
    items = await reg.list_skills()
    assert all(s["source"] in ("system", "user", "project") for s in items)


async def test_factory_single_source_when_path_set(monkeypatch, tmp_path):
    """skills_data_path в config → single-source, только эта папка."""
    custom = tmp_path / "custom_skills"
    custom.mkdir()
    _write_skill(custom, "lonely-skill", "only here")

    # Изолируем env — чтобы случайно не подмешать user/project.
    fake = EnvVars(gc_home=tmp_path / "home")
    monkeypatch.setattr(env_vars, "env", fake)
    monkeypatch.chdir(tmp_path)

    reg = get_skills_registry(make_settings({"skills_data_path": str(custom)}))
    items = await reg.list_skills()
    assert len(items) == 1
    assert items[0]["id"] == "lonely-skill"
    # В single-source режиме все скиллы помечаются как user.
    assert items[0]["source"] == "user"


async def test_factory_single_source_loads_packs(monkeypatch, tmp_path):
    """single-source (skills_data_path) читает и паки, не только flat-скиллы.

    Регрессия: single-source выставлял _walk_packs_enabled=False, поэтому пак
    (папка с pack.yaml, без корневого SKILL.md) молча игнорировался, а
    repo-профиль приносил именно паки (cybergym). Пак должен резолвиться через
    свой bootstrap_skill.
    """
    custom = tmp_path / "custom_skills"
    custom.mkdir()
    _write_skill(custom, "lonely-skill", "only here")  # flat
    pack_dir = custom / "cybergym"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(
        "id: cybergym\nname: CyberGym\nbootstrap_skill: using-cybergym\ndescription: pack\n"
    )
    (pack_dir / "using-cybergym").mkdir()
    (pack_dir / "using-cybergym" / "SKILL.md").write_text(
        "---\nname: using-cybergym\ndescription: entry\n---\n# Using\n"
    )

    fake = EnvVars(gc_home=tmp_path / "home")
    monkeypatch.setattr(env_vars, "env", fake)
    monkeypatch.chdir(tmp_path)

    reg = get_skills_registry(make_settings({"skills_data_path": str(custom)}))
    items = await reg.list_skills()
    ids = {it["id"] for it in items}
    assert "lonely-skill" in ids
    # Пак загрузился, inner-скилл доступен по canonical cybergym:using-cybergym.
    assert "cybergym:using-cybergym" in ids


async def test_factory_empty_skills_data_path_falls_back_to_three_layer(
    monkeypatch, tmp_path
):
    """skills_data_path = "" (пустая строка) → three-layer, не single-source."""
    fake = EnvVars(gc_home=tmp_path / "home")
    monkeypatch.setattr(env_vars, "env", fake)
    monkeypatch.chdir(tmp_path)

    reg = get_skills_registry(make_settings({"skills_data_path": ""}))
    items = await reg.list_skills()
    # System-слой реальный, в three-layer режиме виден.
    assert any(s["source"] == "system" for s in items)
