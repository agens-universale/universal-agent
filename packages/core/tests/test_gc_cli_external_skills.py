"""Tests for profile-skills delivery in the Harbor external adapter.

Requires the `harbor` framework (dependency-group `test`).
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from gc_testkit import as_coro

# tests/harbor/ (a test-fixture package) shadows the real harbor framework:
# reorder sys.path so real site-packages wins, then evict cached shadows.
_harbor_sp = next(
    (
        p
        for p in sys.path
        if (Path(p) / "harbor" / "agents" / "base.py").exists()
    ),
    None,
)
if _harbor_sp:
    sys.path.remove(_harbor_sp)
    sys.path.insert(0, _harbor_sp)
for _key in list(sys.modules.keys()):
    if _key == "harbor" or _key.startswith("harbor."):
        del sys.modules[_key]

from gc_testkit.paths import HARBOR

_HARBOR = HARBOR
sys.path.insert(0, str(_HARBOR))

from adapters.gc_cli_external import GcCliExternal


def _skill(root: Path, name: str) -> Path:
    """Create a minimal skill directory: <root>/<name>/SKILL.md."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n")
    return skill_dir


def _as_async(fn):
    """Заглушка сборки контекста стала корутинной вместе со швом адаптера."""

    async def _wrapped(*args, **kwargs):
        return fn(*args, **kwargs)

    return _wrapped


def test_resolve_picks_nested_skill_dirs_and_skips_siblings(tmp_path):
    _skill(tmp_path, "threat-model")
    _skill(tmp_path, "finding-discovery")
    (tmp_path / "notes").mkdir()  # unrelated sibling, no SKILL.md
    (tmp_path / "README.md").write_text("x")

    found = GcCliExternal._resolve_profile_skill_dirs(tmp_path)

    assert [p.name for p in found] == ["finding-discovery", "threat-model"]


def test_resolve_treats_root_with_skill_md_as_one_skill(tmp_path):
    (tmp_path / "SKILL.md").write_text("# autonomous-pentest\n")
    _skill(tmp_path, "ignored-when-root-is-a-skill")

    assert GcCliExternal._resolve_profile_skill_dirs(tmp_path) == [tmp_path]


def test_resolve_treats_root_with_pack_yaml_as_one_pack(tmp_path):
    """Корень с pack.yaml — весь корень один пак, inner-скиллы не дробятся.

    REGRESSION: резолвер знал только корневой SKILL.md; skills_data_path,
    указывающий прямо на корень пака, отдавал inner-скиллы как отдельные
    плоские скиллы, pack.yaml не доезжал до контейнера — терялись pack
    namespace и bootstrap-логика.
    """
    (tmp_path / "pack.yaml").write_text("id: cybergym\n")
    _skill(tmp_path, "using-cybergym")
    (tmp_path / "references").mkdir()  # inner-файлы не превращаются в скиллы

    assert GcCliExternal._resolve_profile_skill_dirs(tmp_path) == [tmp_path]


def test_resolve_returns_empty_for_missing_root(tmp_path):
    assert GcCliExternal._resolve_profile_skill_dirs(tmp_path / "nope") == []


class FakeEnv:
    """Records container-side calls. Mirrors tests/test_harbor_backend.py::FakeEnv."""

    def __init__(self, existing_dirs=()):
        self.existing_dirs = set(existing_dirs)
        self.execs: list[str] = []
        self.uploads: list[tuple[str, str]] = []

    async def exec(self, command, **kw):
        self.execs.append(command)
        return None

    async def is_dir(self, path, user=None):
        return path in self.existing_dirs

    async def upload_dir(self, source_dir, target_dir):
        self.uploads.append((str(source_dir), target_dir))
        self.existing_dirs.add(target_dir)


def _agent(**overrides) -> GcCliExternal:
    cfg = {"completions_url": "http://test/v1/chat/completions"}
    cfg.update(overrides)
    return GcCliExternal(
        logs_dir=Path("/tmp"), model_name="openai/m", gc_cli_config=cfg
    )


def test_merge_uploads_profile_skills_into_harbor_root(tmp_path):
    _skill(tmp_path, "threat-model")
    _skill(tmp_path, "validation")
    agent = _agent(skills_data_path=str(tmp_path))
    env = FakeEnv(existing_dirs={"/harbor/skills"})

    root, sources = asyncio.run(
        agent._merge_profile_skills(env, "/harbor/skills")
    )

    assert root == "/harbor/skills"
    assert sources == {"threat-model": "profile", "validation": "profile"}
    assert [t for _, t in env.uploads] == [
        "/harbor/skills/threat-model",
        "/harbor/skills/validation",
    ]


def test_merge_uploads_pack_root_whole(tmp_path):
    """skills_data_path = сам корень пака → upload всей директории.

    pack.yaml обязан доехать до контейнера: без него registry в runner
    потеряет pack namespace и bootstrap-логику.
    """
    (tmp_path / "pack.yaml").write_text("id: cybergym\n")
    _skill(tmp_path, "using-cybergym")
    agent = _agent(skills_data_path=str(tmp_path))
    env = FakeEnv(existing_dirs={"/harbor/skills"})

    root, sources = asyncio.run(
        agent._merge_profile_skills(env, "/harbor/skills")
    )

    assert root == "/harbor/skills"
    assert sources == {tmp_path.name: "profile"}
    assert [t for _, t in env.uploads] == [f"/harbor/skills/{tmp_path.name}"]


def test_merge_skips_name_already_present_in_container(tmp_path):
    _skill(tmp_path, "mesh-analysis")
    _skill(tmp_path, "validation")
    agent = _agent(skills_data_path=str(tmp_path))
    # the task already injected mesh-analysis
    env = FakeEnv(
        existing_dirs={"/harbor/skills", "/harbor/skills/mesh-analysis"}
    )

    root, sources = asyncio.run(
        agent._merge_profile_skills(env, "/harbor/skills")
    )

    assert sources == {"mesh-analysis": "skipped", "validation": "profile"}
    assert [t for _, t in env.uploads] == ["/harbor/skills/validation"]


def test_merge_creates_default_root_when_harbor_injected_nothing(tmp_path):
    _skill(tmp_path, "validation")
    agent = _agent(skills_data_path=str(tmp_path))
    env = FakeEnv()

    root, sources = asyncio.run(agent._merge_profile_skills(env, None))

    assert root == "/harbor/skills"
    assert "mkdir -p /harbor/skills" in env.execs
    assert sources == {"validation": "profile"}


def test_merge_raises_when_profile_dir_missing_on_runner(tmp_path):
    agent = _agent(skills_data_path=str(tmp_path / "nope"))
    env = FakeEnv()

    with pytest.raises(RuntimeError, match="does not exist on the runner"):
        asyncio.run(agent._merge_profile_skills(env, "/harbor/skills"))

    assert env.uploads == []


def test_merge_warns_and_uploads_nothing_when_no_skill_md(tmp_path):
    (tmp_path / "notes").mkdir()
    agent = _agent(skills_data_path=str(tmp_path))
    env = FakeEnv(existing_dirs={"/harbor/skills"})

    root, sources = asyncio.run(
        agent._merge_profile_skills(env, "/harbor/skills")
    )

    assert (root, sources) == ("/harbor/skills", {})
    assert env.uploads == []


from types import SimpleNamespace


def _stub_core(monkeypatch, captured: dict):
    """Capture the config the core would receive; skip the real agent loop."""

    class StubExecutor:
        def __init__(self, ctx):
            captured["config"] = ctx.config_dict

        # Адаптер ведёт агента своим `asyncio.run(executor.arun(...))`:
        # синхронного `run` на этом пути больше нет.
        async def arun(self, messages):
            return None

        def get_state(self):
            return SimpleNamespace(messages=[])

    monkeypatch.setattr(
        "gc_core.run_context.acreate_run_context",
        _as_async(
            lambda cfg, **kw: SimpleNamespace(config_dict=cfg, os_backend=None)
        ),
    )
    monkeypatch.setattr("gc_core.agent_executor.AgentExecutor", StubExecutor)


def test_run_merges_profile_skills_and_points_config_at_container_root(
    tmp_path, monkeypatch
):
    _skill(tmp_path, "threat-model")
    agent = GcCliExternal(
        logs_dir=tmp_path / "logs",
        model_name="openai/m",
        gc_cli_config={
            "completions_url": "http://test/v1/chat/completions",
            "skills_data_path": str(tmp_path),
        },
    )
    env = FakeEnv(existing_dirs={"/harbor/skills"})
    agent.skills_dir = "/harbor/skills"
    captured: dict = {}
    _stub_core(monkeypatch, captured)

    asyncio.run(
        agent.run(
            "do X", environment=env, context=SimpleNamespace(metadata=None)
        )
    )

    assert captured["config"]["skills_data_path"] == "/harbor/skills"
    assert [t for _, t in env.uploads] == ["/harbor/skills/threat-model"]


def test_run_merges_profile_skills_without_backend_special_casing(
    tmp_path, monkeypatch
):
    _skill(tmp_path, "threat-model")
    agent = GcCliExternal(
        logs_dir=tmp_path / "logs",
        model_name="openai/m",
        gc_cli_config={
            "completions_url": "http://test/v1/chat/completions",
            "skills_data_path": str(tmp_path),
        },
    )
    env = FakeEnv(existing_dirs={"/harbor/skills"})
    agent.skills_dir = "/harbor/skills"
    captured: dict = {}
    _stub_core(monkeypatch, captured)

    asyncio.run(
        agent.run(
            "do X", environment=env, context=SimpleNamespace(metadata=None)
        )
    )

    assert [t for _, t in env.uploads] == ["/harbor/skills/threat-model"]
    assert captured["config"]["skills_data_path"] == "/harbor/skills"


async def test_skills_debug_artifact_records_loaded_ids_and_sources(
    tmp_path, monkeypatch
):
    agent = GcCliExternal(
        logs_dir=tmp_path / "logs",
        model_name="openai/m",
        gc_cli_config={"completions_url": "http://test/v1/chat/completions"},
    )
    monkeypatch.setattr(
        "gc_core.tools.skills.registry.get_skills_registry",
        lambda cfg, backend=None: SimpleNamespace(
            list_skills=as_coro(
                lambda: [{"id": "threat-model"}, {"id": "mesh-analysis"}]
            )
        ),
    )
    ctx = SimpleNamespace(
        config_dict={"skills_data_path": "/harbor/skills"}, os_backend=None
    )

    await agent._write_skills_debug(
        ctx, {"threat-model": "profile", "validation": "skipped"}
    )

    payload = json.loads((tmp_path / "logs" / "skills.json").read_text())
    assert payload["skills_root"] == "/harbor/skills"
    assert payload["loaded"] == [
        {"id": "mesh-analysis", "source": "task"},
        {"id": "threat-model", "source": "profile"},
    ]
    assert payload["skipped"] == ["validation"]
    assert payload["counts"] == {"loaded": 2, "profile": 1, "skipped": 1}


async def test_skills_debug_artifact_marks_collision_source_as_task(
    tmp_path, monkeypatch
):
    agent = GcCliExternal(
        logs_dir=tmp_path / "logs",
        model_name="openai/m",
        gc_cli_config={"completions_url": "http://test/v1/chat/completions"},
    )
    monkeypatch.setattr(
        "gc_core.tools.skills.registry.get_skills_registry",
        lambda cfg, backend=None: SimpleNamespace(
            list_skills=as_coro(lambda: [{"id": "threat-model"}])
        ),
    )
    ctx = SimpleNamespace(
        config_dict={"skills_data_path": "/harbor/skills"}, os_backend=None
    )

    # "threat-model" collided: the task already provided a skill with this name, so
    # the profile's copy was skipped, but the registry still loads *a* skill under
    # this name — the task's copy. The artifact must attribute that loaded copy to
    # "task", not mislabel it "skipped" just because the profile's upload was skipped.
    await agent._write_skills_debug(ctx, {"threat-model": "skipped"})

    payload = json.loads((tmp_path / "logs" / "skills.json").read_text())
    assert payload["loaded"] == [{"id": "threat-model", "source": "task"}]
    assert payload["skipped"] == ["threat-model"]


async def test_skills_debug_pack_source_via_real_registry(tmp_path):
    """Полный путь: profile pack → registry → skills.json с source: profile.

    REGRESSION: sources ключуются именем загруженной директории (cybergym),
    а registry отдаёт канонический id пачного скилла (cybergym:using-cybergym).
    Атрибуция по точному id промахивалась и писала source: task — файл
    противоречил собственным счётчикам. Матч должен идти по root_path записи.
    """
    pack = tmp_path / "cybergym"
    pack.mkdir()
    (pack / "pack.yaml").write_text(
        "id: cybergym\nbootstrap_skill: using-cybergym\n"
    )
    (pack / "using-cybergym").mkdir()
    (pack / "using-cybergym" / "SKILL.md").write_text(
        "---\nname: using-cybergym\ndescription: entry\n---\n# Using\n"
    )
    _skill(tmp_path, "threat-model")  # flat-скилл рядом с паком

    agent = GcCliExternal(
        logs_dir=tmp_path / "logs",
        model_name="openai/m",
        gc_cli_config={"completions_url": "http://test/v1/chat/completions"},
    )
    # Реальный registry (single-source) без стабов — как в проде.
    ctx = SimpleNamespace(
        config_dict={"skills_data_path": str(tmp_path)}, os_backend=None
    )

    await agent._write_skills_debug(
        ctx, {"cybergym": "profile", "threat-model": "profile"}
    )

    payload = json.loads((tmp_path / "logs" / "skills.json").read_text())
    assert payload["loaded"] == [
        {"id": "cybergym:using-cybergym", "source": "profile"},
        {"id": "threat-model", "source": "profile"},
    ]
    assert payload["counts"] == {"loaded": 2, "profile": 2, "skipped": 0}


async def test_skills_debug_artifact_failure_does_not_raise(
    tmp_path, monkeypatch
):
    agent = GcCliExternal(
        logs_dir=tmp_path / "logs",
        model_name="openai/m",
        gc_cli_config={"completions_url": "http://test/v1/chat/completions"},
    )

    def _boom(cfg, backend=None):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(
        "gc_core.tools.skills.registry.get_skills_registry", _boom
    )

    await agent._write_skills_debug(
        SimpleNamespace(config_dict={}, os_backend=None), {}
    )

    assert not (tmp_path / "logs" / "skills.json").exists()
