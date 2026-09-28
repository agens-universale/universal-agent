from gc_core.tools.skills.skill_delivery import build_skill_delivery


class _Reg:
    async def load_skill_detail(self, skill_id):
        return {
            "instructions": "DO X",
            "references": ["ref_a"],
            "assets": ["img_b"],
        }


async def test_delivery_is_english_and_structured():
    out = await build_skill_delivery("demo", _Reg())
    # directive + lock — на английском
    assert "Use skill `demo`." in out
    assert "is already loaded" in out
    assert "do not call skills_load again" in out
    assert (
        "Available references (load by exact name via skills_read_reference): ref_a"
        in out
    )
    assert (
        "Available assets (load by exact name via skills_read_asset): img_b"
        in out
    )
    assert "Load auxiliary files by the exact names above" in out
    assert "Do not read skill files via read_file." not in out
    # инструкция скилла сохранена
    assert "DO X" in out
    # никакой кириллицы в wire-тексте
    assert not any("Ѐ" <= ch <= "ӿ" for ch in out)


async def test_delivery_fallback_on_unknown_skill():
    class _Bad:
        async def load_skill_detail(self, skill_id):
            raise KeyError(skill_id)

    assert await build_skill_delivery("x", _Bad()) == "Use skill `x`."
