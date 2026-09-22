import pytest
from gc_core.tools.skills.registry import SkillsRegistry

class _Skill:
    def __init__(self, sid): self.id = sid

def _registry(ids):
    r = SkillsRegistry.__new__(SkillsRegistry)
    r._skills_by_id = {sid: _Skill(sid) for sid in ids}
    async def _noop():
        return None

    r._ensure_loaded = _noop                 # тест не грузит ФС
    r.reload = lambda: None
    r._resolve_pack_bootstrap = lambda s: None
    return r

async def test_doubled_namespace_resolves_to_flat():
    r = _registry(["qa-tc-generation"])
    skill = await r._lookup_skill("qa-tc-generation:qa-tc-generation")
    assert skill.id == "qa-tc-generation"

async def test_not_found_lists_candidates():
    r = _registry(["qa-tc-generation"])
    with pytest.raises(KeyError) as ei:
        await r._lookup_skill("pack:qa-tc-generatio")  # опечатка хвоста
    assert "qa-tc-generation" in str(ei.value)  # подсказка-кандидат
