"""Tests for skill packs in the new geometry (packs inside skills/, pack.yaml marker)."""

from pathlib import Path

import pytest
from gc_testkit.settings import make_settings


def _write_flat_skill(
    layer_root: Path, skill_id: str, description: str = ""
) -> Path:
    """Создать <layer>/<skill-id>/SKILL.md — обычный flat-скилл."""
    skill_dir = layer_root / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f'---\nid: {skill_id}\nname: {skill_id}\ndescription: "{description}"\n---\n# body of {skill_id}\n',
        encoding="utf-8",
    )
    return skill_dir


def _write_pack(
    layer_root: Path,
    pack_id: str,
    *,
    inner_skills: list[str],
    bootstrap_skill: str | None,
    manifest_extras: dict | None = None,
) -> Path:
    """
    Создать пакет <layer>/<pack_id>/ с pack.yaml и подпапками для inner-скиллов.

    bootstrap_skill: если задан — попадает в pack.yaml. Если None — pack.yaml
    создаётся без этого поля (пакет будет "невидим" в default list_skills).
    """
    import yaml

    pack_dir = layer_root / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)

    manifest = {"id": pack_id}
    if bootstrap_skill is not None:
        manifest["bootstrap_skill"] = bootstrap_skill
    if manifest_extras:
        manifest.update(manifest_extras)
    (pack_dir / "pack.yaml").write_text(
        yaml.safe_dump(manifest), encoding="utf-8"
    )

    for inner_id in inner_skills:
        inner_dir = pack_dir / inner_id
        inner_dir.mkdir(parents=True, exist_ok=True)
        (inner_dir / "SKILL.md").write_text(
            f'---\nid: {inner_id}\nname: {inner_id}\ndescription: ""\n---\n# body\n',
            encoding="utf-8",
        )
    return pack_dir


def _registry(tmp_path, *, with_layers=("system",)):
    """Build a SkillsRegistry pointing at <tmp_path>/<layer>/ for chosen layers, empty for others."""
    from gc_core.tools.skills.registry import SkillsRegistry

    paths = {
        "system": tmp_path / "system"
        if "system" in with_layers
        else tmp_path / "system_empty",
        "user": tmp_path / "user"
        if "user" in with_layers
        else tmp_path / "user_empty",
        "project": tmp_path / "project"
        if "project" in with_layers
        else tmp_path / "project_empty",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return SkillsRegistry(
        system_root=str(paths["system"]),
        user_root=str(paths["user"]),
        project_root=str(paths["project"]),
    ), paths


async def test_discriminator_flat_skill_only(tmp_path):
    """Папка с SKILL.md и без pack.yaml — обычный flat-скилл."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    _write_flat_skill(paths["system"], "swe-task-solver", "do swe tasks")
    items = {i["id"]: i for i in await reg.list_skills()}
    assert "swe-task-solver" in items
    assert items["swe-task-solver"]["pack_id"] is None
    assert items["swe-task-solver"]["is_pack_bootstrap"] is False
    assert "pack_size" not in items["swe-task-solver"]


async def test_discriminator_pack_only(tmp_path):
    """Папка с pack.yaml — пакет, не flat-скилл."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    _write_pack(
        paths["system"],
        "superpowers",
        inner_skills=["using-superpowers", "brainstorming"],
        bootstrap_skill="using-superpowers",
    )
    items = {i["id"]: i for i in await reg.list_skills()}
    # Только bootstrap в default-выдаче
    assert "superpowers:using-superpowers" in items
    # Internal не виден
    assert "superpowers:brainstorming" not in items
    # И сама папка пакета НЕ скилл (нет canonical "superpowers" без двоеточия)
    assert "superpowers" not in items


async def test_discriminator_pack_and_skill_md_on_root_warns_treats_as_pack(
    tmp_path, caplog
):
    """Папка с pack.yaml И SKILL.md на корне — гибрид; warn + трактуется как пакет, корневой SKILL.md игнорируется."""
    import logging

    reg, paths = _registry(tmp_path, with_layers=("system",))
    pack_dir = _write_pack(
        paths["system"],
        "hybrid",
        inner_skills=["using-it"],
        bootstrap_skill="using-it",
    )
    # Добавляем SKILL.md на корне пакета — это ошибочный гибрид
    (pack_dir / "SKILL.md").write_text(
        '---\nid: hybrid\nname: hybrid\ndescription: "should be ignored"\n---\n# body\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        items = {i["id"]: i for i in await reg.list_skills()}
    # Пакет, а не flat
    assert "hybrid:using-it" in items
    assert (
        "hybrid" not in items
    )  # SKILL.md на корне НЕ регистрируется как flat
    # Warn в логах (точный текст не зашиваем, проверяем что что-то логировалось)
    assert any(
        "hybrid" in record.getMessage().lower() for record in caplog.records
    )


async def test_discriminator_empty_directory_silently_skipped(tmp_path):
    """Папка без SKILL.md и без pack.yaml — молча скипается."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    (paths["system"] / "empty-dir").mkdir()
    _write_flat_skill(paths["system"], "real-skill", "")
    items = {i["id"]: i for i in await reg.list_skills()}
    assert "real-skill" in items
    assert "empty-dir" not in items


async def test_filter_default_hides_internal_skills(tmp_path):
    """По умолчанию internal-скиллы скрыты, виден только bootstrap."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    _write_pack(
        paths["system"],
        "superpowers",
        inner_skills=["using-superpowers", "brainstorming", "writing-plans"],
        bootstrap_skill="using-superpowers",
    )
    items = await reg.list_skills()
    ids = {i["id"] for i in items}
    assert ids == {"superpowers:using-superpowers"}


async def test_filter_with_pack_param_returns_all_pack_internals(tmp_path):
    """list_skills(pack='X') возвращает все internal этого пакета (включая bootstrap)."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    _write_pack(
        paths["system"],
        "superpowers",
        inner_skills=["using-superpowers", "brainstorming", "writing-plans"],
        bootstrap_skill="using-superpowers",
    )
    items = await reg.list_skills(pack="superpowers")
    ids = {i["id"] for i in items}
    assert ids == {
        "superpowers:using-superpowers",
        "superpowers:brainstorming",
        "superpowers:writing-plans",
    }


async def test_filter_with_pack_param_unknown_pack_returns_empty(tmp_path):
    """list_skills(pack='nonexistent') не падает, возвращает []."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    _write_pack(
        paths["system"],
        "superpowers",
        inner_skills=["using-superpowers"],
        bootstrap_skill="using-superpowers",
    )
    assert await reg.list_skills(pack="nonexistent") == []


async def test_filter_include_all_returns_everything(tmp_path):
    """include_all=True возвращает flat + bootstrap + internal."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    _write_flat_skill(paths["system"], "swe-task-solver", "")
    _write_pack(
        paths["system"],
        "superpowers",
        inner_skills=["using-superpowers", "brainstorming"],
        bootstrap_skill="using-superpowers",
    )
    items = await reg.list_skills(include_all=True)
    ids = {i["id"] for i in items}
    assert ids == {
        "swe-task-solver",
        "superpowers:using-superpowers",
        "superpowers:brainstorming",
    }


async def test_filter_pack_overrides_include_all(tmp_path):
    """pack='X' имеет приоритет над include_all=True (более специфичный фильтр)."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    _write_flat_skill(paths["system"], "swe-task-solver", "")
    _write_pack(
        paths["system"],
        "superpowers",
        inner_skills=["using-superpowers", "brainstorming"],
        bootstrap_skill="using-superpowers",
    )
    items = await reg.list_skills(pack="superpowers", include_all=True)
    ids = {i["id"] for i in items}
    # flat не показывается, потому что pack=superpowers фильтрует к одному пакету
    assert ids == {
        "superpowers:using-superpowers",
        "superpowers:brainstorming",
    }


async def test_bootstrap_has_is_pack_bootstrap_true_and_pack_size(tmp_path):
    """Bootstrap entry получает is_pack_bootstrap=true и pack_size=N."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    _write_pack(
        paths["system"],
        "superpowers",
        inner_skills=["using-superpowers", "brainstorming", "writing-plans"],
        bootstrap_skill="using-superpowers",
    )
    items = {i["id"]: i for i in await reg.list_skills()}
    bootstrap = items["superpowers:using-superpowers"]
    assert bootstrap["is_pack_bootstrap"] is True
    assert bootstrap["pack_size"] == 3
    assert bootstrap["pack_id"] == "superpowers"


async def test_internal_non_bootstrap_has_is_pack_bootstrap_false_no_pack_size(
    tmp_path,
):
    """Internal non-bootstrap скиллы получают is_pack_bootstrap=false, без pack_size."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    _write_pack(
        paths["system"],
        "superpowers",
        inner_skills=["using-superpowers", "brainstorming"],
        bootstrap_skill="using-superpowers",
    )
    items = {i["id"]: i for i in await reg.list_skills(pack="superpowers")}
    inner = items["superpowers:brainstorming"]
    assert inner["is_pack_bootstrap"] is False
    assert "pack_size" not in inner


async def test_flat_skill_has_is_pack_bootstrap_false_no_pack_size(tmp_path):
    """Flat-скиллы получают is_pack_bootstrap=false, без pack_size."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    _write_flat_skill(paths["system"], "swe-task-solver", "")
    items = {i["id"]: i for i in await reg.list_skills()}
    flat = items["swe-task-solver"]
    assert flat["is_pack_bootstrap"] is False
    assert "pack_size" not in flat


async def test_internal_skill_standalone_loadable_despite_hidden(tmp_path):
    """skills_load("pack:inner") работает даже если inner скрыт из default list."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    _write_pack(
        paths["system"],
        "superpowers",
        inner_skills=["using-superpowers", "brainstorming"],
        bootstrap_skill="using-superpowers",
    )
    # brainstorming не виден в default list_skills
    default_ids = {i["id"] for i in await reg.list_skills()}
    assert "superpowers:brainstorming" not in default_ids
    # Но грузится напрямую
    detail = await reg.load_skill_detail("superpowers:brainstorming")
    assert detail["id"] == "superpowers:brainstorming"


async def test_short_alias_finds_hidden_internal(tmp_path):
    """skills_load("brainstorming") (без префикса) находит uniques superpowers:brainstorming."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    _write_pack(
        paths["system"],
        "superpowers",
        inner_skills=["using-superpowers", "brainstorming"],
        bootstrap_skill="using-superpowers",
    )
    detail = await reg.load_skill_detail("brainstorming")
    assert detail["id"] == "superpowers:brainstorming"


async def test_pack_with_missing_bootstrap_skill_invisible_but_internals_loadable(
    tmp_path,
):
    """Пакет без bootstrap_skill в манифесте: в default list не появляется, но internal грузятся."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    _write_pack(
        paths["system"],
        "broken-pack",
        inner_skills=["inner-a", "inner-b"],
        bootstrap_skill=None,  # манифест без bootstrap_skill
    )
    # В default ни одной записи
    assert await reg.list_skills() == []
    # Но internal-скиллы регистрируются и доступны напрямую
    detail = await reg.load_skill_detail("broken-pack:inner-a")
    assert detail["id"] == "broken-pack:inner-a"
    # И через list_skills(pack=) тоже видны
    pack_items = await reg.list_skills(pack="broken-pack")
    pack_ids = {i["id"] for i in pack_items}
    assert pack_ids == {"broken-pack:inner-a", "broken-pack:inner-b"}


async def test_pack_bootstrap_pointing_to_nonexistent_skill_invisible(
    tmp_path,
):
    """Пакет с bootstrap_skill, указывающим в никуда: невидим, но internal доступны."""
    reg, paths = _registry(tmp_path, with_layers=("system",))
    _write_pack(
        paths["system"],
        "misconfigured",
        inner_skills=["actual-inner"],
        bootstrap_skill="nonexistent-entry",  # такого скилла нет
    )
    assert await reg.list_skills() == []
    # Internal доступен напрямую
    detail = await reg.load_skill_detail("misconfigured:actual-inner")
    assert detail["id"] == "misconfigured:actual-inner"


async def test_override_inner_skill_project_layer(tmp_path):
    """project-layer переопределяет internal-скилл пакета system-слоя."""
    reg, paths = _registry(tmp_path, with_layers=("system", "project"))
    # System-слой: пакет с internal-скиллом
    _write_pack(
        paths["system"],
        "superpowers",
        inner_skills=["brainstorming"],
        bootstrap_skill="brainstorming",
    )
    # Project-слой: переопределяет brainstorming. ВАЖНО: project тоже должен
    # содержать pack.yaml, иначе папка не считается пакетом.
    _write_pack(
        paths["project"],
        "superpowers",
        inner_skills=["brainstorming"],
        bootstrap_skill="brainstorming",
    )
    # Перезаписываем body project-версии чтобы был отличим
    (
        paths["project"] / "superpowers" / "brainstorming" / "SKILL.md"
    ).write_text(
        '---\nid: brainstorming\nname: brainstorming\ndescription: "from project"\n---\n# project version\n',
        encoding="utf-8",
    )
    detail = await reg.load_skill_detail("superpowers:brainstorming")
    assert detail["source"] == "project"
    assert "from project" in detail["description"]


async def test_single_source_loads_packs(tmp_path):
    """skills_data_path в config → single-source режим читает и паки тоже.

    REGRESSION: раньше single-source выставлял _walk_packs_enabled=False, и пак
    (папка с pack.yaml, без корневого SKILL.md) молча игнорировался, а
    repo-профиль приносил именно паки (cybergym).
    """
    from gc_core.tools.skills.registry import get_skills_registry

    custom = tmp_path / "custom"
    custom.mkdir()
    # Кладём папку с pack.yaml — в single-source должна стать пакетом
    _write_pack(
        custom,
        "would-be-pack",
        inner_skills=["inner-a"],
        bootstrap_skill="inner-a",
    )
    # И flat-скилл
    _write_flat_skill(custom, "flat-skill", "")

    reg = get_skills_registry(
        make_settings({"paths.skills_data_path": str(custom)})
    )
    items = {i["id"] for i in await reg.list_skills()}
    # Flat-скилл виден, пак загрузился: bootstrap доступен по canonical id
    assert "flat-skill" in items
    assert "would-be-pack:inner-a" in items


async def test_bootstrap_resolved_by_folder_name_when_frontmatter_id_differs(
    tmp_path,
):
    """
    REGRESSION: pack.yaml::bootstrap_skill может ссылаться на имя ПАПКИ или
    на frontmatter id entry-скилла. Folder-name match — fallback когда прямой
    canonical lookup пропускает (frontmatter id во SKILL.md отличается от
    имени папки).

    Без этого fallback'а пакет с такой конфигурацией silently invisible.
    """
    import yaml
    from gc_core.tools.skills.registry import SkillsRegistry

    layer = tmp_path / "system"
    layer.mkdir()
    pack_dir = layer / "my-pack"
    pack_dir.mkdir()
    # Inner skill: folder "entry/" но frontmatter id "actual-entry"
    inner_dir = pack_dir / "entry"
    inner_dir.mkdir()
    (inner_dir / "SKILL.md").write_text(
        '---\nid: actual-entry\nname: actual-entry\ndescription: ""\n---\n# body\n',
        encoding="utf-8",
    )
    # Manifest ссылается на имя папки, не на frontmatter id
    (pack_dir / "pack.yaml").write_text(
        yaml.safe_dump({"id": "my-pack", "bootstrap_skill": "entry"}),
        encoding="utf-8",
    )

    reg = SkillsRegistry(
        system_root=str(layer),
        user_root=str(tmp_path / "u_empty"),
        project_root=str(tmp_path / "p_empty"),
    )
    items = {i["id"]: i for i in await reg.list_skills()}
    # Pack должен быть видим — bootstrap резолвится через folder-name fallback.
    # Canonical скилла строится из frontmatter id, не из имени папки.
    assert "my-pack:actual-entry" in items
    bootstrap = items["my-pack:actual-entry"]
    assert bootstrap["is_pack_bootstrap"] is True
    assert bootstrap["pack_size"] == 1


async def test_bootstrap_direct_canonical_wins_over_folder_name_match(
    tmp_path,
):
    """
    Если есть два скилла где folder-name одного совпадает с frontmatter-id
    другого, прямой canonical lookup выигрывает: bootstrap_skill: bar →
    matches skill с frontmatter id 'bar', не skill в папке 'bar/'.
    """
    import yaml
    from gc_core.tools.skills.registry import SkillsRegistry

    layer = tmp_path / "system"
    layer.mkdir()
    pack_dir = layer / "ambig"
    pack_dir.mkdir()
    # Skill A: folder "foo/", frontmatter id "bar" (canonical: ambig:bar)
    (pack_dir / "foo").mkdir()
    (pack_dir / "foo" / "SKILL.md").write_text(
        '---\nid: bar\nname: bar\ndescription: "A"\n---\n# A\n',
        encoding="utf-8",
    )
    # Skill B: folder "bar/", frontmatter id "baz" (canonical: ambig:baz)
    (pack_dir / "bar").mkdir()
    (pack_dir / "bar" / "SKILL.md").write_text(
        '---\nid: baz\nname: baz\ndescription: "B"\n---\n# B\n',
        encoding="utf-8",
    )
    # bootstrap_skill: bar — должен матчиться через прямой canonical lookup
    # с скиллом A (его frontmatter id = bar), НЕ с скиллом B (его folder = bar).
    (pack_dir / "pack.yaml").write_text(
        yaml.safe_dump({"id": "ambig", "bootstrap_skill": "bar"}),
        encoding="utf-8",
    )

    reg = SkillsRegistry(
        system_root=str(layer),
        user_root=str(tmp_path / "u_empty"),
        project_root=str(tmp_path / "p_empty"),
    )
    items = {i["id"]: i for i in await reg.list_skills()}
    # Виден bootstrap — скилл A (canonical ambig:bar) по прямому lookup'у.
    assert "ambig:bar" in items
    assert items["ambig:bar"]["is_pack_bootstrap"] is True
    # Skill B скрыт (не bootstrap, internal)
    assert "ambig:baz" not in items


async def test_bare_pack_id_resolves_to_bootstrap(tmp_path):
    """A bare pack-id loads the pack's bootstrap (entry) skill instead of 404 (hop1)."""
    _write_pack(
        tmp_path / "system",
        "superpowers",
        inner_skills=["using-superpowers", "brainstorming"],
        bootstrap_skill="using-superpowers",
    )
    reg, _ = _registry(tmp_path)
    detail = await reg.load_skill_detail("superpowers")
    assert detail["id"] == "superpowers:using-superpowers"
    assert detail["pack_id"] == "superpowers"


async def test_bare_pack_id_underscore_variant_resolves(tmp_path):
    """Bare pack-id with underscores normalizes to the hyphen pack id."""
    _write_pack(
        tmp_path / "system",
        "science-article-creator",
        inner_skills=["using-science-article-creator", "scientific-research"],
        bootstrap_skill="using-science-article-creator",
    )
    reg, _ = _registry(tmp_path)
    detail = await reg.load_skill_detail("science_article_creator")
    assert (
        detail["id"] == "science-article-creator:using-science-article-creator"
    )


async def test_bare_pack_id_without_bootstrap_still_raises(tmp_path):
    """A pack that declares no bootstrap_skill does not resolve by bare id."""
    _write_pack(
        tmp_path / "system",
        "nobs",
        inner_skills=["inner-a"],
        bootstrap_skill=None,
    )
    reg, _ = _registry(tmp_path)
    with pytest.raises(KeyError):
        await reg.load_skill_detail("nobs")


async def test_unknown_bare_id_still_raises(tmp_path):
    """Non-pack, non-skill ids still raise KeyError (no regression)."""
    _write_flat_skill(tmp_path / "system", "swe-task-solver")
    reg, _ = _registry(tmp_path)
    with pytest.raises(KeyError):
        await reg.load_skill_detail("totally-bogus-xyz")
