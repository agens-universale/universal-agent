import sys

# `import cli` parses sys.argv at import time (--query is required); pin a
# valid argv so this file runs deterministically under any pytest invocation
# (e.g. `pytest tests/test_cli_skill_flag.py::test_x` without -q).
sys.argv = ["gc-cli", "-q", "x"]

import pytest

import cli
from tools.skills.skill_arg_resolver import SkillCatalog


class _FakeReg:
    def __init__(self, cat):
        self._cat = cat

    def skill_catalog(self):
        return self._cat


def _cat():
    return SkillCatalog(
        canonical_ids=frozenset({"supercoder:using-supercoder", "supercoder:brainstorming"}),
        bare_to_canonical={
            "using-supercoder": ("supercoder:using-supercoder",),
            "brainstorming": ("supercoder:brainstorming",),
        },
        pack_bootstrap={"supercoder": "supercoder:using-supercoder"},
    )


@pytest.fixture
def _patch_reg(monkeypatch):
    monkeypatch.setattr(
        "tools.skills.registry.get_skills_registry",
        lambda config_dict=None: _FakeReg(_cat()),
    )


def test_empty_is_noop(_patch_reg):
    assert cli._resolve_forced_skill("", {}) is None
    assert cli._resolve_forced_skill("   ", {}) is None


def test_empty_does_not_build_registry(monkeypatch):
    def _boom(config_dict=None):
        raise AssertionError("registry must not be built when -s is empty")

    monkeypatch.setattr("tools.skills.registry.get_skills_registry", _boom)
    assert cli._resolve_forced_skill("", {}) is None
    assert cli._resolve_forced_skill("   ", {}) is None


def test_exact_pack_id_resolves_and_notes(_patch_reg, capsys):
    out = cli._resolve_forced_skill("supercoder", {})
    assert out == "supercoder:using-supercoder"
    assert "note:" in capsys.readouterr().err


def test_canonical_resolves_no_note(_patch_reg, capsys):
    out = cli._resolve_forced_skill("supercoder:brainstorming", {})
    assert out == "supercoder:brainstorming"
    assert "note:" not in capsys.readouterr().err


def test_typo_resolves_with_note(_patch_reg, capsys):
    out = cli._resolve_forced_skill("suprcoder", {})
    assert out == "supercoder:using-supercoder"
    assert "note:" in capsys.readouterr().err


def test_not_found_soft_fallback_warns(_patch_reg, capsys):
    out = cli._resolve_forced_skill("zzzzzzzz", {})
    assert out == "zzzzzzzz"
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "not_found" in err


def test_ambiguous_soft_fallback_warns_with_suggestions(monkeypatch, capsys):
    cat = SkillCatalog(
        canonical_ids=frozenset({"a:dup", "b:dup"}),
        bare_to_canonical={"dup": ("a:dup", "b:dup")},
        pack_bootstrap={},
    )
    monkeypatch.setattr(
        "tools.skills.registry.get_skills_registry",
        lambda config_dict=None: _FakeReg(cat),
    )
    out = cli._resolve_forced_skill("dup", {})
    assert out == "dup"
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "ambiguous" in err
    assert "a:dup" in err and "b:dup" in err


def test_fuzzy_ambiguous_soft_fallback_warns(monkeypatch, capsys):
    cat = SkillCatalog(
        canonical_ids=frozenset({"p:foo-a", "p:foo-b"}),
        bare_to_canonical={"foo-a": ("p:foo-a",), "foo-b": ("p:foo-b",)},
        pack_bootstrap={},
    )
    monkeypatch.setattr(
        "tools.skills.registry.get_skills_registry",
        lambda config_dict=None: _FakeReg(cat),
    )
    out = cli._resolve_forced_skill("foo-x", {})
    assert out == "foo-x"
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "fuzzy_ambiguous" in err
    assert "foo-a" in err and "foo-b" in err


def test_catalog_build_failure_soft_fallback_warns(monkeypatch, capsys):
    def _boom(config_dict=None):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("tools.skills.registry.get_skills_registry", _boom)
    out = cli._resolve_forced_skill("supercoder", {})
    assert out == "supercoder"
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "kaboom" in err
