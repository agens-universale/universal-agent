"""Скиллы в среде исполнения: копия слоёв через бэкенд и реестр по ним."""

import pytest

from gc_core.tools.skills import registry as registry_module
from gc_core.tools.skills.provision import (
    MANIFEST_NAME,
    RUNTIME_SUBDIR,
    aprovision_remote_skills,
    should_provision,
)
from gc_testkit.harbor_fakes import FakeBackend


class RemoteFs(FakeBackend):
    """Удалённая ФС с cwd /work и считающим exec (rm -rf)."""

    def __init__(self, files=None):
        super().__init__(files)
        self.exec_commands = []
        self.writes = 0

    def default_cwd(self):
        return "/work"

    async def aexec(self, command, **kwargs):
        from types import SimpleNamespace

        self.exec_commands.append(command)
        if command.startswith("rm -rf "):
            prefix = command[len("rm -rf ") :].strip("'") + "/"
            for key in [k for k in self.files if k.startswith(prefix)]:
                del self.files[key]
        return SimpleNamespace(
            stdout="", stderr="", return_code=0, timed_out=False
        )

    async def aexec_argv(self, argv, **kwargs):
        raise AssertionError

    async def arun_python(self, code, **kwargs):
        raise AssertionError

    async def write_bytes(self, p, data):
        self.writes += 1
        await super().write_bytes(p, data)


def _skill(root, layer_dir, skill_id, body="do things"):
    d = root / layer_dir / skill_id
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: {body}\n---\n# {skill_id}\n{body}\n",
        encoding="utf-8",
    )
    (d / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    return d


@pytest.fixture
def host_layers(tmp_path, monkeypatch):
    _skill(tmp_path, "sys", "alpha")
    _skill(tmp_path, "usr", "beta")
    _skill(tmp_path, "proj", "alpha", body="project override")
    monkeypatch.setattr(
        registry_module, "_system_skills_root", lambda: str(tmp_path / "sys")
    )
    monkeypatch.setattr(
        registry_module, "_user_skills_root", lambda: str(tmp_path / "usr")
    )
    monkeypatch.setattr(
        registry_module, "_project_skills_root", lambda: str(tmp_path / "proj")
    )
    return tmp_path


async def test_provisions_three_layers_and_registry_reads_remote(host_layers):
    backend = RemoteFs()
    cfg = {"launch_root": "/work", "os_backend": {"kind": "docker"}}
    registry = await aprovision_remote_skills(backend, cfg)
    assert registry is not None
    root = f"/work/{RUNTIME_SUBDIR}"
    assert f"{root}/system/alpha/SKILL.md" in backend.files
    assert f"{root}/user/beta/scripts/run.py" in backend.files
    assert f"{root}/project/alpha/SKILL.md" in backend.files
    assert f"{root}/{MANIFEST_NAME}" in backend.files
    assert backend.exec_commands == [f"rm -rf {root}"]

    skills = {s["id"]: s for s in await registry.list_skills(include_all=True)}
    assert set(skills) == {"alpha", "beta"}
    # project перекрывает system и живёт по удалённому пути
    alpha = await registry.load_skill_detail("alpha")
    assert alpha["root_path"] == f"{root}/project/alpha"
    assert "project override" in alpha["instructions"]
    beta = await registry.load_skill_detail("beta")
    assert beta["root_path"] == f"{root}/user/beta"
    assert beta["has_scripts"] is True


async def test_second_run_with_same_set_copies_nothing(host_layers):
    backend = RemoteFs()
    cfg = {"launch_root": "/work"}
    await aprovision_remote_skills(backend, cfg)
    writes, cmds = backend.writes, list(backend.exec_commands)
    await aprovision_remote_skills(backend, cfg)
    assert backend.writes == writes
    assert backend.exec_commands == cmds

    # Изменился файл на хосте → копия пересобирается с нуля
    (host_layers / "usr" / "beta" / "SKILL.md").write_text(
        "---\nname: beta\ndescription: v2\n---\nv2\n", encoding="utf-8"
    )
    await aprovision_remote_skills(backend, cfg)
    assert backend.writes > writes
    assert len(backend.exec_commands) == len(cmds) + 1


async def test_falls_back_to_backend_cwd_without_launch_root(host_layers):
    backend = RemoteFs()
    registry = await aprovision_remote_skills(backend, {})
    assert registry is not None
    assert any(k.startswith(f"/work/{RUNTIME_SUBDIR}/") for k in backend.files)


def test_should_provision_rules():
    class Host:
        is_sandbox = False

    class Remote:
        is_sandbox = True

    assert should_provision({}, Remote()) is True
    assert should_provision({}, Host()) is False
    assert (
        should_provision({"skills_data_path": "/harbor/skills"}, Remote())
        is False
    )
    assert (
        should_provision({"os_backend": {"provision_skills": False}}, Remote())
        is False
    )
    assert should_provision({"os_backend": "docker"}, Remote()) is True


async def test_provisioning_failure_keeps_default_registry(host_layers):
    class Broken(RemoteFs):
        async def write_bytes(self, p, data):
            raise OSError("disk full")

    assert (
        await aprovision_remote_skills(Broken(), {"launch_root": "/work"})
        is None
    )


async def test_run_context_uses_provisioned_registry(host_layers):
    from gc_core.run_context import acreate_run_context

    backend = RemoteFs()
    cfg = {
        "model": "m",
        "system_prompt_id": "v1",
        "save_logs": False,
        "launch_root": "/work",
        "project_root": "/work",
        "os_backend": {"kind": "docker", "docker": {"name": "x"}},
    }
    ctx = await acreate_run_context(cfg, os_backend=backend)
    try:
        detail = await ctx.skills_registry.load_skill_detail("beta")
        assert detail["root_path"] == f"/work/{RUNTIME_SUBDIR}/user/beta"
    finally:
        close = getattr(ctx, "aclose", None)
        if close is not None:
            await close()
