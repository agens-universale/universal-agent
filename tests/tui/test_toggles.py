"""Тогглы палитры: правило коллизии finish_gate_mode с глобальным рубильником.

Правило (спека §3): явный tui-ключ побеждает; иначе явный
agent.strict_turn_finish_validation=false даёт off; иначе smart (дефолт medium).
Владелец правила — tui/session_settings.py (его читают и витрина тоггла, и
сравнение с умолчанием)."""
import pytest

from tui.session_settings import effective_finish_gate_mode


def _cfg(mode="on", explicit=False, strict_false=False):
    return {
        "tui_finish_gate_mode": mode,
        "tui_finish_gate_mode_explicit": explicit,
        "strict_turn_finish_explicit_false": strict_false,
    }


def test_both_defaults_is_smart():
    assert effective_finish_gate_mode(_cfg()) == "smart"


def test_explicit_global_off_wins_over_default_tui():
    """Старая воля юзера (strict=false руками) уважается: апдейт молча не включает судью."""
    assert effective_finish_gate_mode(_cfg(strict_false=True)) == "off"


def test_explicit_tui_beats_explicit_global_off():
    assert effective_finish_gate_mode(_cfg(mode="on", explicit=True, strict_false=True)) == "on"


def test_explicit_tui_off():
    assert effective_finish_gate_mode(_cfg(mode="off", explicit=True)) == "off"


def test_missing_keys_default_smart():
    assert effective_finish_gate_mode({}) == "smart"


def test_template_yaml_default_is_string_not_bool(tmp_path):
    """YAML-ловушка: голый on/off = bool True/False. Шаблоны пишут значение в кавычках; схема принимает."""
    from pathlib import Path

    import yaml
    from config import _default_config_path
    from config_modules.tui import TuiConfig

    repo_root = Path(__file__).resolve().parents[2]
    for tpl in (repo_root / "demo_config.yml", Path(_default_config_path())):
        data = yaml.safe_load(tpl.read_text(encoding="utf-8"))
        v = data["tui"]["finish_gate_mode"]
        assert v == "smart" and isinstance(v, str), f"{tpl}: {v!r}"
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


def test_apply_finish_gate_mode_smart_sets_both_runtime_keys():
    from tui.toggles import apply_finish_gate_mode
    cfg = {"tui_finish_gate_mode": "smart", "tui_finish_gate_mode_explicit": True}
    apply_finish_gate_mode(cfg)
    assert cfg["strict_turn_finish_validation"] is True
    assert cfg["finish_gate_smart"] is True


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
    assert gate.names == ("off", "smart", "on")
    off = gate.values[0]
    assert off.value == "off"
    assert off.color == pal.INK            # gate off — белый (спека §3)
    on = gate.values[2]
    assert on.value == "on"
    assert on.color == pal.ACCENT
    assert "самопроверка" in on.hint
    ans = TOGGLES["answers"]
    assert ans.names == ("allow", "judge", "deny")
    assert ans.values[1].color == pal.RUN_COLOR   # judge — амбер
    assert ans.values[0].color == pal.INK          # allow — белый
    assert "вызовом инструмента" in ans.values[2].hint


def test_cycle_value_still_works_with_value_objects():
    from tui.toggles import cycle_value
    assert cycle_value("answers", "judge", 1) == "deny"
    assert cycle_value("answers", "deny", -1) == "judge"


def test_toggle_specs_carry_label_and_summary():
    from tui.toggles import TOGGLES
    assert TOGGLES["gate"].label == "самопроверка выполнения задачи"
    assert TOGGLES["gate"].summary == "закрыт ли запрос"   # ревизия 4: короче, стабильность колонок
    assert TOGGLES["answers"].label == "формат ответа"
    assert TOGGLES["answers"].summary == "допустим ли текст как финал хода"
    assert "самопроверку" in TOGGLES["answers"].values[1].hint  # judge обновлён


def test_render_track_canonical():
    from tui.toggles import TOGGLES, render_track
    t = render_track(TOGGLES["answers"], "judge")
    assert t.plain == "○ ● ○"
    t2 = render_track(TOGGLES["gate"], "on")
    assert t2.plain == "○ ○ ●"


def test_gate_gains_smart_value():
    from tui.toggles import TOGGLES
    from tui import palette as pal
    gate = TOGGLES["gate"]
    assert gate.names == ("off", "smart", "on")     # off·smart·on (мягкое→строгое, §3)
    smart = gate.values[1]
    assert smart.value == "smart" and smart.color == pal.RUN_COLOR
    assert "работал инструментами" in smart.hint


def test_rigor_toggle_spec_shape():
    from tui.toggles import TOGGLES, RIGOR_KEYS, resolve_rigor_state
    rigor = TOGGLES["rigor"]
    assert rigor.names == ("low", "medium", "high")
    assert rigor.preset_keys == RIGOR_KEYS
    assert rigor.state is resolve_rigor_state
    assert rigor.palette_default is True
    assert rigor.dotted == ""
    # low INK, medium RUN_COLOR, high ACCENT (спека §4.1)
    from tui import palette as pal
    assert rigor.values[0].color == pal.INK
    assert rigor.values[1].color == pal.RUN_COLOR
    assert rigor.values[2].color == pal.ACCENT


def test_rigor_is_first_and_details_hidden_by_default():
    from tui.toggles import TOGGLES, group_toggle_names
    # rigor — первый в СВОЕЙ группе (спека §12.1: /level и /profiles — новая
    # группа «чем работаем» выше rigor, отдельная семья, group_toggle_names).
    assert group_toggle_names("rigor")[0] == "rigor"
    assert TOGGLES["gate"].palette_default is False
    assert TOGGLES["answers"].palette_default is False


def test_rigor_description_from_spec():
    from tui.toggles import TOGGLES
    r = TOGGLES["rigor"]
    assert r.label == "строгость завершения хода"
    assert r.summary == "самопроверка и формат ответа"


def test_render_track_extra_position_none_is_bit_identical():
    from tui.toggles import TOGGLES, render_track
    a = render_track(TOGGLES["answers"], "judge")
    b = render_track(TOGGLES["answers"], "judge", extra_lit=None)
    assert a.plain == b.plain == "○ ● ○"


def test_render_track_extra_reserved_and_lit():
    """Ревизия 4 (спека §2, §4.1): False теперь РЕЗЕРВ — двойной пробел + один
    ПРОБЕЛ (3 пустых ячейки), без тусклой ○ (никакого намёка на выбираемую
    позицию, custom больше не выбирается стрелкой). True не изменился: база
    вся тусклая, 4-я — применённая ● BRAND."""
    from tui.toggles import TOGGLES, render_track
    reserved = render_track(TOGGLES["rigor"], "low", extra_lit=False)
    assert reserved.plain == "● ○ ○   "        # база + двойной пробел + 1 пустая ячейка
    assert "○" not in reserved.plain[len("● ○ ○"):]
    custom = render_track(TOGGLES["rigor"], "custom", extra_lit=True)
    assert custom.plain == "○ ○ ○  ●"


def test_next_toggle_value_generic_for_single_key():
    from tui.toggles import next_toggle_value

    class _App:
        _config = {"tui_text_answer_mode": "judge"}
        def effective_config(self):
            return dict(self._config)

    assert next_toggle_value(_App(), "answers", 1) == "deny"
    assert next_toggle_value(_App(), "answers", -1) == "allow"


# --- Ревизия 4 (спека §2, §4.1): custom — только отображение, не выбор -----


# --- Ревизия 5 (спека §6): padded_track — левое выравнивание, системный контракт ---


def test_padded_track_no_padding_needed_matches_render_track():
    """base_w/extra_w уже равны собственной ширине строки (реальные rigor/gate —
    трек 3-значный у обоих) — `padded_track` не добавляет ничего лишнего,
    совпадает с `render_track` бит-в-бит."""
    from tui.toggles import TOGGLES, padded_track, render_track

    t = padded_track(TOGGLES["gate"], "on", extra_lit=None, base_w=5, extra_w=0)
    assert t.plain == render_track(TOGGLES["gate"], "on", extra_lit=None).plain == "○ ○ ●"


def test_padded_track_pads_narrower_base_to_common_field_on_the_right():
    """Контракт левого края (ревизия 5, спека §6): более узкий базовый трек
    паддится пробелами СПРАВА до общего `base_w` — первый глиф остаётся на
    месте (левый край), а не сдвигается вправо (старое поведение)."""
    from tui import palette as p
    from tui.toggles import ToggleSpec, ToggleValue, padded_track

    two = ToggleSpec(
        values=(ToggleValue("a", "", p.INK), ToggleValue("b", "", p.ACCENT)),
        state=lambda cfg: "a", label="", summary="",
    )
    t = padded_track(two, "a", extra_lit=None, base_w=5, extra_w=0)
    assert t.plain == "● ○" + "  "          # собственная база(3) + 2 пробела до base_w=5
    assert t.plain.index("●") == 0           # глиф остался у левого края


def test_padded_track_reserves_blank_extra_zone_for_row_without_own_extra():
    """Ребёнок пресета (`extra_lit=None`, своего резерва нет) в общем поле, где
    у родителя резерв есть (`extra_w=3`, спека §6, «custom-резерв правее
    троек, даже у строк без своего резерва»): база не сдвинута, зона резерва
    целиком пуста (никакого намёка на глиф)."""
    from tui.toggles import TOGGLES, padded_track

    t = padded_track(TOGGLES["gate"], "on", extra_lit=None, base_w=5, extra_w=3)
    assert t.plain == "○ ○ ●" + "   "
    assert "●" not in t.plain[5:] and "○" not in t.plain[5:]


def test_padded_track_extra_lit_true_still_bold_brand_after_padding():
    """Собственный custom-резерв (`extra_lit=True`) — паддинг не трогает сам
    БРЕНД-глиф и его стиль, только достраивает поле, если `extra_w` шире
    собственного резерва."""
    from tui import palette as p
    from tui.toggles import TOGGLES, padded_track

    t = padded_track(TOGGLES["rigor"], "custom", extra_lit=True, base_w=5, extra_w=3)
    assert t.plain == "○ ○ ○  ●"
    assert any(
        "bold" in str(s.style).lower() and p.BRAND.lower() in str(s.style).lower()
        for s in t.spans
    )


def test_next_toggle_value_displayed_custom_falls_back_to_idx_zero():
    """`current` kwarg удалён (ревизия 4): отправная точка всегда читается
    через `toggle_state(app, name)`. Эффективное значение вне `spec.names`
    (отображаемый custom) трактуется как idx 0 — шаг уводит на пресет по
    кругу от начала шкалы, не от «4-й позиции» (её больше нет)."""
    from tui.toggles import next_toggle_value

    class _App:
        _config = {   # пара нестандартна -> эффективное "custom"
            "tui_finish_gate_mode": "on", "tui_finish_gate_mode_explicit": True,
            "tui_text_answer_mode": "allow",
        }
        def effective_config(self):
            return dict(self._config)

    assert next_toggle_value(_App(), "rigor", 1) == "medium"    # idx0 +1
    assert next_toggle_value(_App(), "rigor", -1) == "high"     # idx0 -1 (wrap)


# --- Ревизия 8 (спека §3): answers синхронизирован с осью мягкое→строгое ---


def test_answers_track_synchronized_with_gate_axis():
    """Инвариант всех треков (спека §3, ревизия 8): слева-направо от мягкого/
    дешёвого (INK) к строгому/тщательному (ACCENT). answers исторически лежал
    задом наперёд (deny·judge·allow) и расходился с gate/rigor в пресетах —
    разворачивается в allow·judge·deny с цветами INK·RUN_COLOR·ACCENT. Синхрон:
    для каждого пресета позиция answers-значения в его треке == позиции
    gate-значения в его треке (оба идут low/off слева -> high/on справа)."""
    from tui import palette as pal
    from tui.toggles import PRESETS, TOGGLES

    answers = TOGGLES["answers"]
    assert answers.names == ("allow", "judge", "deny")
    assert answers.values[0].color == pal.INK
    assert answers.values[1].color == pal.RUN_COLOR
    assert answers.values[2].color == pal.ACCENT

    gate = TOGGLES["gate"]
    for pair in PRESETS.values():
        gate_value = pair["tui.finish_gate_mode"]
        answers_value = pair["tui.text_answer_mode"]
        assert gate.names.index(gate_value) == answers.names.index(answers_value)
