"""Скиллы в среде исполнения: копия слоёв через бэкенд и реестр по ним."""

import shlex
from types import SimpleNamespace

import pytest

from gc_core.tools.skills import registry as registry_module
from gc_core.tools.skills.provision import (
    MANIFEST_NAME,
    RUNTIME_SUBDIR,
    aprovision_remote_skills,
    should_provision,
)
from gc_testkit.harbor_fakes import FakeBackend

DOCKER = {"kind": "docker", "docker": {"name": "c"}}


class RemoteFs(FakeBackend):
    """Удалённая ФС с cwd /work и exec, понимающим `rm -rf ROOT && mv STAGED ROOT`."""

    def __init__(self, files=None):
        super().__init__(files)
        self.exec_commands = []
        self.writes = 0

    def default_cwd(self):
        return "/work"

    async def aexec(self, command, **kwargs):
        self.exec_commands.append(command)
        rm_part, mv_part = command.split(" && ")
        root = shlex.split(rm_part)[2]
        staged = shlex.split(mv_part)[1]
        for key in [k for k in self.files if k.startswith(root + "/")]:
            del self.files[key]
        for key in [k for k in self.files if k.startswith(staged + "/")]:
            self.files[root + key[len(staged) :]] = self.files.pop(key)
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
    cfg = {"launch_root": "/work", "os_backend": DOCKER}
    registry = await aprovision_remote_skills(backend, cfg)
    assert registry is not None
    root = f"/work/{RUNTIME_SUBDIR}"
    assert f"{root}/system/alpha/SKILL.md" in backend.files
    assert f"{root}/user/beta/scripts/run.py" in backend.files
    assert f"{root}/project/alpha/SKILL.md" in backend.files
    assert f"{root}/{MANIFEST_NAME}" in backend.files
    # одна подмена живой копии, staging-каталога после неё нет
    assert len(backend.exec_commands) == 1
    assert backend.exec_commands[0].startswith(
        f"rm -rf {root} && mv {root}.staging-"
    )
    assert not any(".staging-" in k for k in backend.files)

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
    cfg = {
        "launch_root": "/work",
        "os_backend": {"kind": "ssh", "ssh": {"host": "h"}},
    }
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


async def test_root_fallbacks_project_root_then_backend_cwd(host_layers):
    backend = RemoteFs()
    exec_cfg = {"kind": "exec", "exec": {"argv_prefix": ["x"]}}
    await aprovision_remote_skills(
        backend, {"project_root": "/proj", "os_backend": exec_cfg}
    )
    assert any(k.startswith(f"/proj/{RUNTIME_SUBDIR}/") for k in backend.files)

    backend = RemoteFs()
    registry = await aprovision_remote_skills(
        backend, {"os_backend": exec_cfg}
    )
    assert registry is not None
    assert any(k.startswith(f"/work/{RUNTIME_SUBDIR}/") for k in backend.files)


def test_should_provision_rules():
    class Host:
        is_sandbox = False

    class Remote:
        is_sandbox = True

    docker = {"os_backend": DOCKER}
    assert should_provision(docker, Remote()) is True
    assert should_provision(docker, Host()) is False
    assert (
        should_provision(
            {**docker, "skills_data_path": "/harbor/skills"}, Remote()
        )
        is False
    )
    assert (
        should_provision(
            {"os_backend": {**DOCKER, "provision_skills": False}}, Remote()
        )
        is False
    )
    # harbor грузит адаптером, local/sandbox читают хост — не трогаем
    assert should_provision({"os_backend": "harbor"}, Remote()) is False
    assert should_provision({}, Remote()) is False
    assert should_provision({"os_backend": "sandbox"}, Remote()) is False


async def test_symlinked_files_are_not_uploaded(host_layers, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("creds", encoding="utf-8")
    (host_layers / "usr" / "beta" / "scripts" / "leak.txt").symlink_to(secret)
    backend = RemoteFs()
    await aprovision_remote_skills(
        backend, {"launch_root": "/work", "os_backend": DOCKER}
    )
    assert not any(k.endswith("/leak.txt") for k in backend.files)
    assert "creds" not in backend.files.values()


async def test_provisioning_failure_keeps_default_registry(host_layers):
    class Broken(RemoteFs):
        async def write_bytes(self, p, data):
            raise OSError("disk full")

    cfg = {"launch_root": "/work", "os_backend": DOCKER}
    assert await aprovision_remote_skills(Broken(), cfg) is None


async def test_run_context_uses_provisioned_registry(host_layers):
    from gc_core.run_context import acreate_run_context

    backend = RemoteFs()
    cfg = {
        "model": "m",
        "system_prompt_id": "v1",
        "save_logs": False,
        "launch_root": "/work",
        "project_root": "/work",
        "os_backend": DOCKER,
    }
    ctx = await acreate_run_context(cfg, os_backend=backend)
    detail = await ctx.skills_registry.load_skill_detail("beta")
    assert detail["root_path"] == f"/work/{RUNTIME_SUBDIR}/user/beta"
