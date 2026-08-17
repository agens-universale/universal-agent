"""При неверном slug ошибка load_reference/load_asset перечисляет доступные —
чтобы модель само-исправилась, а не гадала имена дальше."""
import pytest

from gc_core.tools.skills.registry import SkillsRegistry


def _reg_with_refs(tmp_path):
    system = tmp_path / "system"
    skill = system / "qa-req-validation"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: qa-req-validation\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    (skill / "references" / "quality_criteria.md").write_text("qc", encoding="utf-8")
    (skill / "references" / "report_templates.md").write_text("rt", encoding="utf-8")
    return SkillsRegistry(system_root=str(system))


def test_load_reference_miss_lists_available(tmp_path):
    reg = _reg_with_refs(tmp_path)
    with pytest.raises(KeyError) as ei:
        reg.load_reference("qa-req-validation", "manual")  # выдуманный slug
    msg = str(ei.value)
    assert "quality_criteria" in msg
    assert "report_templates" in msg


def test_load_reference_hit_still_works(tmp_path):
    reg = _reg_with_refs(tmp_path)
    res = reg.load_reference("qa-req-validation", "quality_criteria")
    assert res["slug"] == "quality_criteria"
    assert res["content"] == "qc"
