"""Native registration of harbor-injected task skills without host mirroring."""

import sys
import tempfile
from pathlib import Path

from gc_testkit.paths import HARBOR, REPO

_REPO = REPO

# Expose `adapters/` (inside ./harbor) and `src/` for the adapter import. Append, do
# not prepend: repo-root `./harbor` must not shadow the installed `harbor` pip package
# on sys.path. These tests live under tests/harbor_support/ (not tests/harbor/) for the
# same reason — a package named `harbor` would collide with the pip lib. In the real
# bench PYTHONPATH=/opt/gc-harbor-adapters, so `adapters` is top-level with no collision.
for p in (str(HARBOR), str(_REPO / "src")):
    if p not in sys.path:
        sys.path.append(p)

from adapters.gc_cli_external import GcCliExternal
from gc_testkit.settings import make_settings


def test_resolve_profile_skill_dirs_includes_packs():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "flat").mkdir()
        (root / "flat" / "SKILL.md").write_text("# flat")
        (root / "cybergym").mkdir()
        (root / "cybergym" / "pack.yaml").write_text("id: cybergym\n")
        (root / "cybergym" / "using-cybergym").mkdir()
        (root / "cybergym" / "using-cybergym" / "SKILL.md").write_text("# use")
        (root / "junk").mkdir()  # no SKILL.md / pack.yaml -> skipped
        dirs = [
            p.name for p in GcCliExternal._resolve_profile_skill_dirs(root)
        ]
        assert dirs == ["cybergym", "flat"], dirs


def test_apply_injected_skills_uses_container_path():
    settings = make_settings(skills_data_path="/opt/gc-harbor-adapters/skills")
    applied = GcCliExternal._apply_injected_skills(settings, "/harbor/skills")
    assert applied.settings.paths.skills_data_path == "/harbor/skills"


def test_apply_injected_skills_without_container_skills_uses_empty_sentinel():
    settings = make_settings(skills_data_path="/opt/gc-harbor-adapters/skills")
    applied = GcCliExternal._apply_injected_skills(settings, None)
    assert applied.settings.paths.skills_data_path == "/harbor/.gc_no_skills"


def test_native_nudge_added_only_when_skills():
    assert "skills_list" in GcCliExternal._nudge_instruction(
        "do X", "/harbor/skills"
    )
    assert GcCliExternal._nudge_instruction("do X", None) == "do X"
