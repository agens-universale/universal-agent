"""Тогглы палитры: правило коллизии finish_gate_mode с глобальным рубильником.

Правило (спека §3): явный tui-ключ побеждает; иначе явный
agent.strict_turn_finish_validation=false даёт off; иначе on."""
import pytest

from tui.toggles import effective_finish_gate_mode


def _cfg(mode="on", explicit=False, strict_false=False):
    return {
        "tui_finish_gate_mode": mode,
        "tui_finish_gate_mode_explicit": explicit,
        "strict_turn_finish_explicit_false": strict_false,
    }


def test_both_defaults_is_on():
    assert effective_finish_gate_mode(_cfg()) == "on"


def test_explicit_global_off_wins_over_default_tui():
    """Старая воля юзера (strict=false руками) уважается: апдейт молча не включает судью."""
    assert effective_finish_gate_mode(_cfg(strict_false=True)) == "off"


def test_explicit_tui_beats_explicit_global_off():
    assert effective_finish_gate_mode(_cfg(mode="on", explicit=True, strict_false=True)) == "on"


def test_explicit_tui_off():
    assert effective_finish_gate_mode(_cfg(mode="off", explicit=True)) == "off"


def test_missing_keys_default_on():
    assert effective_finish_gate_mode({}) == "on"


def test_template_yaml_on_is_string_not_bool(tmp_path):
    """YAML-ловушка: голый on = bool True. Шаблоны пишут "on" в кавычках; схема принимает."""
    from pathlib import Path

    import yaml
    from config import _default_config_path
    from config_modules.tui import TuiConfig

    repo_root = Path(__file__).resolve().parents[2]
    for tpl in (repo_root / "demo_config.yml", Path(_default_config_path())):
        data = yaml.safe_load(tpl.read_text(encoding="utf-8"))
        v = data["tui"]["finish_gate_mode"]
        assert v == "on" and isinstance(v, str), f"{tpl}: {v!r}"
    assert TuiConfig(finish_gate_mode="on").finish_gate_mode == "on"


def test_tui_wiring_projects_off_into_strict_flag():
    """Эффективный off проецируется в существующий runtime-ключ строгости —
    guard.enabled (strict AND chat_completions) гаснет существующей веткой."""
    from tui.toggles import apply_finish_gate_mode
    cfg = _cfg(mode="off", explicit=True)
    cfg["strict_turn_finish_validation"] = True     # из flatten (глобальный дефолт)
    apply_finish_gate_mode(cfg)
    assert cfg["strict_turn_finish_validation"] is False


def test_tui_wiring_on_keeps_strict_true():
    from tui.toggles import apply_finish_gate_mode
    cfg = _cfg(mode="on", explicit=True, strict_false=True)   # явный tui on поверх глобального off
    cfg["strict_turn_finish_validation"] = False
    apply_finish_gate_mode(cfg)
    assert cfg["strict_turn_finish_validation"] is True


# --- §7 test 4a: коллизия через реальный config.yml (load_config/flatten_app_config) ---


def _load(tmp_path, monkeypatch, yaml_text):
    """load_config на изолированном config.yml (overlay-слои нейтрализованы — тот же
    паттерн, что test_flatten_exposes_tui_text_answer_mode в test_adaptive_bounce_flag.py)."""
    import config as cfgmod
    import mcp_runtime.paths as mpaths

    monkeypatch.setattr(cfgmod, "_project_local_config_path", lambda: None)
    monkeypatch.setattr(mpaths, "managed_config_path", lambda: tmp_path / "none.yml")

    conf = tmp_path / "config.yml"
    conf.write_text(yaml_text, encoding="utf-8")
    return cfgmod.load_config(str(conf))


def test_explicit_global_off_without_tui_key_is_off_e2e(tmp_path, monkeypatch):
    """agent.strict_turn_finish_validation: false явно, tui.finish_gate_mode отсутствует
    → через реальный load_config/flatten_app_config эффективный /gate = off."""
    cfg = _load(tmp_path, monkeypatch, "agent:\n  strict_turn_finish_validation: false\n")
    assert effective_finish_gate_mode(cfg) == "off"


def test_explicit_tui_key_beats_explicit_global_off_e2e(tmp_path, monkeypatch):
    """Тот же явный global off + явный tui.finish_gate_mode: on → tui-ключ побеждает."""
    cfg = _load(
        tmp_path,
        monkeypatch,
        'agent:\n  strict_turn_finish_validation: false\ntui:\n  finish_gate_mode: "on"\n',
    )
    assert effective_finish_gate_mode(cfg) == "on"


def test_toggle_values_carry_hint_and_color():
    from tui.toggles import TOGGLES
    from tui import palette as pal
    gate = TOGGLES["gate"]
    assert gate.names == ("off", "on")     # порядок = позиции трека: off слева, on справа (спека §4)
    off = gate.values[0]
    assert off.value == "off"
    assert off.color == pal.INK            # gate off — белый (спека §3)
    on = gate.values[1]
    assert on.value == "on"
    assert on.color == pal.ACCENT
    assert "самопроверка" in on.hint
    ans = TOGGLES["answers"]
    assert ans.names == ("deny", "judge", "allow")
    assert ans.values[1].color == pal.RUN_COLOR   # judge — амбер
    assert ans.values[0].color == pal.INK          # deny — белый
    assert "вызовом инструмента" in ans.values[0].hint


def test_cycle_value_still_works_with_value_objects():
    from tui.toggles import cycle_value
    assert cycle_value("answers", "judge", 1) == "allow"
    assert cycle_value("answers", "deny", -1) == "allow"


def test_toggle_specs_carry_label_and_summary():
    from tui.toggles import TOGGLES
    assert TOGGLES["gate"].label == "самопроверка выполнения задачи"
    assert TOGGLES["gate"].summary == "закрыт ли запрос пользователя"
    assert TOGGLES["answers"].label == "формат ответа"
    assert TOGGLES["answers"].summary == "допустим ли текст как финал хода"
    assert "самопроверку" in TOGGLES["answers"].values[1].hint  # judge обновлён


def test_render_track_canonical():
    from tui.toggles import TOGGLES, render_track
    t = render_track(TOGGLES["answers"], "judge")
    assert t.plain == "○ ● ○"
    t2 = render_track(TOGGLES["gate"], "on")
    assert t2.plain == "○ ●"
