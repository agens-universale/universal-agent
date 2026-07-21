"""Native registration of harbor-injected task skills in gc_cli_external.

harbor uploads a task's skills into the container at ``skills_dir``; the adapter mirrors
them to the runner at the same path and points gc-cli's registry (skills_data_path) there
so skills_list / skills_load(skill_id) resolve them. These tests exercise that wiring
against a fake environment (no real container needed).
"""
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# Expose `adapters/` (inside ./harbor) and `src/` for the adapter import. Append, do
# not prepend: repo-root `./harbor` must not shadow the installed `harbor` pip package
# on sys.path. These tests live under tests/harbor_support/ (not tests/harbor/) for the
# same reason — a package named `harbor` would collide with the pip lib. In the real
# bench PYTHONPATH=/opt/gc-harbor-adapters, so `adapters` is top-level with no collision.
for p in (str(_REPO / "harbor"), str(_REPO / "src")):
    if p not in sys.path:
        sys.path.append(p)

from adapters.gc_cli_external import GcCliExternal  # noqa: E402


class _FakeEnv:
    """Records download_dir calls; is_dir true only for the injected skills path."""

    def __init__(self, skills_dir):
        self._skills_dir = skills_dir
        self.downloads = []  # list[(source_dir, str(target_dir))]

    async def is_dir(self, path, user=None):
        return path == self._skills_dir

    async def download_dir(self, source_dir, target_dir):
        self.downloads.append((source_dir, str(target_dir)))


def _agent(skills_dir):
    a = object.__new__(GcCliExternal)  # skip BaseAgent.__init__
    a.skills_dir = skills_dir
    return a


@pytest.mark.asyncio
async def test_mirror_downloads_to_runner_temp_and_returns_it():
    env = _FakeEnv("/harbor/skills")
    result = await _agent("/harbor/skills")._mirror_injected_skills(env)
    assert result is not None
    assert len(env.downloads) == 1
    src, dst = env.downloads[0]
    assert src == "/harbor/skills"     # copied FROM the container path
    assert dst == result               # TO the returned runner-local dir
    # a fresh writable runner path, NOT the container /harbor path (which the host lacks)
    assert result != "/harbor/skills"


@pytest.mark.asyncio
async def test_mirror_no_skills_dir_returns_none_and_no_download():
    env = _FakeEnv("/harbor/skills")
    assert await _agent(None)._mirror_injected_skills(env) is None
    assert env.downloads == []


@pytest.mark.asyncio
async def test_mirror_absent_dir_returns_none():
    # skills_dir set but the container has no such dir → nothing to mirror
    env = _FakeEnv("/other")
    assert await _agent("/harbor/skills")._mirror_injected_skills(env) is None
    assert env.downloads == []


def test_apply_injected_skills_overrides_data_path():
    cfg = {"skills_data_path": "/opt/gc-harbor-adapters/skills"}
    GcCliExternal._apply_injected_skills(cfg, "/harbor/skills")
    assert cfg["skills_data_path"] == "/harbor/skills"


def test_apply_injected_skills_noop_when_none():
    cfg = {"skills_data_path": "/opt/gc-harbor-adapters/skills"}
    GcCliExternal._apply_injected_skills(cfg, None)
    assert cfg["skills_data_path"] == "/opt/gc-harbor-adapters/skills"


def test_native_nudge_added_only_when_skills():
    assert "skills_list" in GcCliExternal._nudge_instruction("do X", "/harbor/skills")
    assert GcCliExternal._nudge_instruction("do X", None) == "do X"


if __name__ == "__main__":
    import asyncio

    asyncio.run(test_mirror_downloads_to_runner_temp_and_returns_it())
    asyncio.run(test_mirror_no_skills_dir_returns_none_and_no_download())
    asyncio.run(test_mirror_absent_dir_returns_none())
    test_apply_injected_skills_overrides_data_path()
    test_apply_injected_skills_noop_when_none()
    test_native_nudge_added_only_when_skills()
    print("ok")
