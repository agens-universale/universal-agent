import pytest
from gc_core.tools.skills.registry import SkillsRegistry

class _Skill:
    def __init__(self, sid): self.id = sid

def _registry(ids):
    r = SkillsRegistry.__new__(SkillsRegistry)
    r._skills_by_id = {sid: _Skill(sid) for sid in ids}
    r._ensure_loaded = lambda: None          # тест не грузит ФС
    r.reload = lambda: None
    r._resolve_pack_bootstrap = lambda s: None
    return r

def test_doubled_namespace_resolves_to_flat():
    r = _registry(["qa-tc-generation"])
    skill = r._lookup_skill("qa-tc-generation:qa-tc-generation")
    assert skill.id == "qa-tc-generation"

def test_not_found_lists_candidates():
    r = _registry(["qa-tc-generation"])
    with pytest.raises(KeyError) as ei:
        r._lookup_skill("pack:qa-tc-generatio")  # опечатка хвоста
    assert "qa-tc-generation" in str(ei.value)  # подсказка-кандидат
