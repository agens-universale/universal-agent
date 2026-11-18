"""Skills плагинов приходят из снимка ``RuntimeEnv.plugins``."""

import asyncio
import inspect

from gc_core.plugins.snapshot import PluginSnapshot, SkillRoot
from gc_core.tools.skills import registry as registry_module
from gc_core.tools.skills.registry import get_skills_registry
from gc_testkit.settings import make_settings

SKILL = "---\nname: {name}\ndescription: d\n---\nbody\n"


def _skill(root, name):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SKILL.format(name=name), encoding="utf-8")


def _loaded(*roots):
    return make_settings().with_runtime(
        plugins=PluginSnapshot(skill_roots=tuple(roots))
    )


def _ids(reg):
    skills = asyncio.run(reg.list_skills())
    return {s["id"] if isinstance(s, dict) else s.id for s in skills}


def test_registry_reads_all_skill_roots_of_plugin(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    _skill(first, "one")
    _skill(second, "two")
    loaded = _loaded(
        SkillRoot("demo", str(first), None),
        SkillRoot("demo", str(second), None),
    )

    ids = _ids(get_skills_registry(loaded, project_root=str(tmp_path / "p")))

    assert {"demo:one", "demo:two"} <= ids


def test_direct_registry_reader_sees_plugin_skills(tmp_path):
    root = tmp_path / "skills"
    _skill(root, "solo")
    loaded = _loaded(SkillRoot("demo", str(root), str(tmp_path)))

    reg = get_skills_registry(loaded, project_root=str(tmp_path / "p"))

    assert "demo:solo" in _ids(reg)


def test_no_snapshot_means_no_plugin_skills(tmp_path):
    reg = get_skills_registry(
        make_settings(), project_root=str(tmp_path / "p")
    )

    assert not any(i.startswith("demo:") for i in _ids(reg))


def test_registry_does_not_import_plugins_module():
    assert "gc_core.plugins" not in inspect.getsource(registry_module)
