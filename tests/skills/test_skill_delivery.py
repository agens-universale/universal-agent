from tools.skills.skill_delivery import build_skill_delivery, skill_is_available


class _FakeReg:
    def __init__(self, detail=None, raises=False):
        self._detail = detail
        self._raises = raises

    def load_skill_detail(self, skill_id):
        if self._raises:
            raise KeyError(skill_id)
        return self._detail


def test_injects_instructions_refs_and_lock():
    reg = _FakeReg({
        "instructions": "ВАЛИДИРУЙ ТРЕБОВАНИЯ ТАК.",
        "references": ["quality_criteria", "report_templates"],
        "assets": [],
    })
    out = build_skill_delivery("qa-req-validation", reg)
    assert out.startswith("Use skill `qa-req-validation`.")
    assert "ВАЛИДИРУЙ ТРЕБОВАНИЯ ТАК." in out
    assert "quality_criteria, report_templates" in out
    assert "skills_read_reference" in out
    assert "do not call skills_load again" in out
    assert "Do not read skill files via read_file." in out


def test_fallback_on_unknown_skill():
    reg = _FakeReg(raises=True)
    out = build_skill_delivery("nope", reg)
    assert out == "Use skill `nope`."


def test_no_reference_clause_when_no_refs_or_assets():
    reg = _FakeReg({"instructions": "X", "references": [], "assets": []})
    out = build_skill_delivery("s", reg)
    assert "skills_read_reference" not in out      # нет вспом. файлов → нет клаузы их загрузки
    assert "Do not read skill files via read_file." in out             # но базовый замок есть
    assert "ВАЛИД" not in out                       # sanity: чужой текст не просочился


def test_assets_listed_when_present():
    reg = _FakeReg({"instructions": "X", "references": [], "assets": ["template_main"]})
    out = build_skill_delivery("s", reg)
    assert "template_main" in out
    assert "skills_read_asset" in out


def test_none_detail_returns_directive_with_empty_instructions():
    reg = _FakeReg(detail=None)
    out = build_skill_delivery("s", reg)
    assert out.startswith("Use skill `s`.")
    assert "skills_read_reference" not in out
    assert "Do not read skill files via read_file." in out


def test_skill_is_available_true_when_detail_loads():
    reg = _FakeReg({"instructions": "x", "references": [], "assets": []})
    assert skill_is_available("s", reg) is True


def test_skill_is_available_false_on_exception():
    reg = _FakeReg(raises=True)
    assert skill_is_available("nope", reg) is False
