"""System-слой скиллов резолвится через bundled_root() — работает и в
editable, и в wheel (где src/ схлопнут и ../../.. промахивается мимо skills/)."""
from pathlib import Path

from gc_core.bundled_assets import bundled_root
from gc_core.tools.skills import registry as registry


def test_system_skills_root_follows_bundled_root(monkeypatch):
    # любой резолв, НЕ опирающийся на bundled_root, провалит этот тест
    # (напр. старый os.path.join(_project_root(), "skills") с ../../..).
    monkeypatch.setattr(registry, "bundled_root", lambda: Path("/fake/root"))
    assert registry._system_skills_root() == str(Path("/fake/root") / "skills")


def test_system_skills_root_exists_in_dev():
    # в реальном окружении встроенные скиллы находятся (каталог есть и непуст)
    root = Path(registry._system_skills_root())
    assert root == bundled_root() / "skills"
    assert root.is_dir()
    assert any(root.iterdir()), "встроенные скиллы не найдены — список будет пуст"
