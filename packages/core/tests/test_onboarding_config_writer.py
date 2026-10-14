from pathlib import Path

import pytest
import yaml
from gc_core import onboarding_config_writer as w


def test_replaces_sentinel_preserving_comment_and_indent():
    text = (
        "backend:\n"
        "  # Впишите endpoint\n"
        '  completions_url: "http://your-backend:PORT/v1/chat/completions"\n'
        "  use_ssl: false\n"
    )
    out = w.set_scalar(
        text, "completions_url", "https://x/v1/chat/completions"
    )
    assert '  completions_url: "https://x/v1/chat/completions"' in out
    assert "# Впишите endpoint" in out  # комментарий сохранён
    assert "use_ssl: false" in out  # соседние строки целы


def test_replaces_empty_value():
    text = 'agent:\n  model: ""\n'
    out = w.set_scalar(text, "model", "qwen3-coder")
    assert '  model: "qwen3-coder"' in out


def test_ignores_commented_line_and_raises_when_absent():
    text = "agent:\n#  model: gigacode_agent_v1.6_rc\n"
    with pytest.raises(KeyError):
        w.set_scalar(text, "model", "x")


def test_escapes_quotes_and_backslashes():
    text = 'h:\n    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
    out = w.set_scalar(text, "Authorization", 'Bearer a"b\\c')
    assert '    Authorization: "Bearer a\\"b\\\\c"' in out


def test_write_values_updates_file(tmp_path):
    """Все ключи нашлись — файл обновлён, соседние строки целы.

    Случай «нашлись не все» проверяет
    `test_write_values_leaves_the_file_untouched_when_a_key_cannot_be_written`: с
    канона 2026-08-20 запись идёт по правилу «всё или ничего»."""
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "backend:\n"
        '  completions_url: "http://your-backend:PORT/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
        "agent:\n"
        '  model: "gigacode_agent_v1.6_rc"\n',
        encoding="utf-8",
    )
    missing = w.write_values(
        cfg,
        {"completions_url": "https://x/v1", "Authorization": "Bearer sk-1"},
    )
    out = cfg.read_text(encoding="utf-8")
    assert missing == []
    assert '  completions_url: "https://x/v1"' in out
    assert '    Authorization: "Bearer sk-1"' in out
    assert '  model: "gigacode_agent_v1.6_rc"' in out  # не тронут


# ── Task 5: §6.4 — write_values разворачивает закомментированный блок шаблона ──


def test_uncomment_template_block_shape():
    tpl = "# Описание секции\n# gizmo:\n#   provider: openai\n#   url: https://x\n"
    block = w.uncomment_template_block(tpl, "gizmo")
    assert "# Описание секции\n" in block  # описание остаётся комментарием
    assert "\ngizmo:\n" in "\n" + block  # шапка раскомментирована
    assert "  provider: openai" in block  # поле — с отступом канона


_TEMPLATE_WITH_COMMENTED_SECTION = 'agent:\n  model: "m"\n\n# Сборка мусора — намеренно выключена из коробки\n#gc:\n#  enabled: true\n'


def test_write_values_expands_commented_block_for_key_it_contains(tmp_path):
    """Охранный: рабочий путь §6.4 — ключ ЕСТЬ в блоке шаблона, блок
    разворачивается и значение пишется."""
    cfg = tmp_path / "config.yml"
    cfg.write_text('agent:\n  model: "m"\n', encoding="utf-8")
    tpl = tmp_path / "config.default.yml"
    tpl.write_text(_TEMPLATE_WITH_COMMENTED_SECTION, encoding="utf-8")

    missing = w.write_values(cfg, {"gc.enabled": "false"}, template_path=tpl)

    out = cfg.read_text(encoding="utf-8")
    assert missing == []
    assert "gc:" in out
    assert "enabled:" in out


def test_write_values_rolls_back_expanded_block_when_key_still_missing(
    tmp_path,
):
    """Ключа нет и в блоке шаблона: `setter` падает KeyError уже ПОСЛЕ того, как
    блок дописан в текст. Ключ уходит в missing — значит и раскомментированная
    секция не должна попасть в файл, иначе юзер молча получает включённой
    секцию, которую сознательно держал выключенной."""
    cfg = tmp_path / "config.yml"
    cfg.write_text('agent:\n  model: "m"\n', encoding="utf-8")
    before = cfg.read_text(encoding="utf-8")
    tpl = tmp_path / "config.default.yml"
    tpl.write_text(_TEMPLATE_WITH_COMMENTED_SECTION, encoding="utf-8")

    missing = w.write_values(cfg, {"gc.threshold": "10"}, template_path=tpl)

    assert missing == ["gc.threshold"]
    assert cfg.read_text(encoding="utf-8") == before


# ── Волна B, Task 1: примитивы записей словаря (models/slots/assign) ──────────


def test_quote_scalar_escapes_quotes_and_backslashes():
    assert w.quote_scalar('a"b\\c') == '"a\\"b\\\\c"'


def test_upsert_mapping_entry_inserts_and_replaces():
    text = "models:\n  glm:\n    url: http://a\nslots:\n  default: glm\n"
    out = w.upsert_mapping_entry(
        text, "models", "flash", ["flash:", "  url: http://b", '  token: ""']
    )
    assert "flash:" in out and "http://b" in out
    out2 = w.upsert_mapping_entry(
        out, "models", "flash", ["flash:", "  url: http://c"]
    )
    assert "http://c" in out2 and "http://b" not in out2
    assert "glm:" in out2  # соседи не тронуты


def test_upsert_mapping_entry_into_empty_bracket_anchor():
    text = "models: {}\nslots:\n  default: glm\n"
    out = w.upsert_mapping_entry(
        text, "models", "flash", ["flash:", "  url: http://b"]
    )
    assert "models: {}" not in out
    assert (
        "models:\n  flash:\n    url: http://b\n" in out
    )  # нормализовано в блочную форму
    assert "slots:\n  default: glm" in out  # сосед не тронут


def test_upsert_mapping_entry_into_bare_empty_anchor():
    text = "models:\nslots:\n  default: glm\n"
    out = w.upsert_mapping_entry(
        text, "models", "flash", ["flash:", "  url: http://b"]
    )
    assert "models:\n  flash:\n    url: http://b\n" in out
    assert "slots:\n  default: glm" in out


def test_upsert_mapping_entry_refuses_non_empty_inline_mapping():
    """Рукописный инлайновый мэппинг (`slots: {default: "flash"}`) переписать
    построчно нечем — своих строк у записей там нет. Нормализация родителя в
    блочную форму молча снесла бы соседей: валидный YAML, зелёный нотис,
    потерянные данные. Отказ — тем же контрактом, что у `_drop_mapping_entry`."""
    text = 'slots: {default: "flash"}\nmodels:\n  flash:\n    url: http://a\n'
    with pytest.raises(ValueError, match="инлайновый мэппинг"):
        w.upsert_mapping_entry(text, "slots", "light", ['light: "glm"'])
    # Ключ, который в этом мэппинге ЕСТЬ, — тот же отказ: «замена» вышла бы
    # переписыванием всей секции, а не одной записи.
    with pytest.raises(ValueError, match="инлайновый мэппинг"):
        w.upsert_mapping_entry(text, "slots", "default", ['default: "glm"'])


def test_upsert_mapping_entry_into_empty_bracket_anchor_keeps_parent_trailing_comment():
    """Нормализация `{}` в блочную форму переписывает СТРОКУ родителя — её
    трейлинг-комментарий описывает секцию и обязан переехать вместе с ней."""
    text = "models: {}   # реестр моделей\nslots:\n  default: glm\n"
    out = w.upsert_mapping_entry(
        text, "models", "flash", ["flash:", "  url: http://b"]
    )
    assert "models:   # реестр моделей\n  flash:\n    url: http://b\n" in out


def test_upsert_mapping_entry_uses_the_column_of_existing_siblings():
    """Отступ новой записи задают УЖЕ ЛЕЖАЩИЕ соседи, а не хардкод «родитель+2»:
    файл с 4-пробельным каноном иначе получал запись на чужой колонке — YAML
    ломался (ParserError) либо запись вкладывалась внутрь соседней."""
    text = 'models:\n    glm:\n        url: "http://a"\n'
    out = w.upsert_mapping_entry(
        text, "models", "flash", ["flash:", '  url: "http://b"']
    )
    assert yaml.safe_load(out)["models"] == {
        "glm": {"url": "http://a"},
        "flash": {"url": "http://b"},
    }


def test_upsert_mapping_entry_missing_parent_raises_keyerror():
    with pytest.raises(KeyError):
        w.upsert_mapping_entry(
            "backend:\n  url: x\n",
            "models",
            "flash",
            ["flash:", "  url: http://b"],
        )


def test_upsert_mapping_entry_replace_preserves_next_entrys_attached_comment():
    """Прикреплённый комментарий СЛЕДУЮЩЕЙ записи принадлежит ей, не заменяемому блоку."""
    text = "models:\n  glm:\n    url: http://a\n  # note about qwen\n  qwen:\n    url: http://c\n"
    out = w.upsert_mapping_entry(
        text, "models", "glm", ["glm:", "  url: http://z"]
    )
    assert "# note about qwen" in out
    assert "qwen:" in out and "http://c" in out
    assert "http://z" in out and "http://a" not in out


def test_upsert_mapping_entry_insert_second_entry_preserves_next_section_attached_comment():
    """Ветка INSERT (записи ещё нет, родитель непуст) защищает прикреплённый
    комментарий СЛЕДУЮЩЕЙ топ-секции так же, как ветка REPLACE. Фикстура — ровно
    та форма (пустая строка + комментарий + топ-ключ), что синтезирует
    `ensure_section_anchor_text`."""
    text = "models:\n  glm:\n    url: http://a\n\n# note about slots\nslots:\n  default: glm\n"
    out = w.upsert_mapping_entry(
        text, "models", "flash", ["flash:", "  url: http://b"]
    )
    assert (
        "# note about slots\nslots:" in out
    )  # комментарий остался НАД slots:, не съехал
    assert "http://b" in out
    assert "glm:" in out  # сосед по models не тронут


def test_upsert_mapping_entry_replace_drops_own_comment_new_block_supplies_its_own():
    """replace = замена БЛОКА записи целиком: старый прикреплённый комментарий
    уходит вместе с ней; новый — первой строкой `block_lines`."""
    text = "models:\n  # old note about flash\n  flash:\n    url: http://b\nslots:\n  default: flash\n"
    out = w.upsert_mapping_entry(
        text, "models", "flash", ["# new note", "flash:", "  url: http://c"]
    )
    assert "old note" not in out
    assert "# new note" in out
    assert "http://c" in out and "http://b" not in out
    assert "slots:\n  default: flash" in out


def test_remove_mapping_entry_keeps_siblings():
    text = "models:\n  glm:\n    url: http://a\n  flash:\n    url: http://b\nslots:\n  default: glm\n"
    out = w.remove_mapping_entry(text, "models", "flash")
    assert "flash" not in out
    assert "glm:" in out and "http://a" in out
    assert "slots:\n  default: glm" in out


def test_remove_mapping_entry_preserves_next_entrys_attached_comment():
    text = "models:\n  glm:\n    url: http://a\n  # note about qwen\n  qwen:\n    url: http://c\n"
    out = w.remove_mapping_entry(text, "models", "glm")
    assert "# note about qwen" in out
    assert "qwen:" in out and "http://c" in out
    assert "glm" not in out


def test_remove_mapping_entry_deletes_own_attached_comment():
    """Собственный комментарий удаляемой записи уходит вместе с ней — иначе
    повисает над следующей, искажая смысл."""
    text = "models:\n  # note about glm\n  glm:\n    url: http://a\n  qwen:\n    url: http://c\n"
    out = w.remove_mapping_entry(text, "models", "glm")
    assert "note about glm" not in out
    assert "qwen:" in out and "http://c" in out


def test_remove_mapping_entry_last_entry_leaves_valid_empty_mapping():
    text = "models:\n  flash:\n    url: http://b\nslots:\n  default: flash\n"
    out = w.remove_mapping_entry(text, "models", "flash")
    assert "models: {}" in out
    assert (
        yaml.safe_load(out)["models"] == {}
    )  # не None — иначе схема упала бы на dict[str, ModelEntry]


def test_remove_mapping_entry_collapse_branch_preserves_next_top_level_comment():
    """Схлопывание в `parent: {}` не должно поглощать прикреплённый комментарий
    СЛЕДУЮЩЕГО топ-уровневого ключа — он принадлежит соседней секции."""
    text = "models:\n  glm:\n    url: http://a\n# note about backend\nbackend:\n  url: http://b\n"
    out = w.remove_mapping_entry(text, "models", "glm")
    assert "models: {}" in out
    assert "# note about backend" in out
    assert "backend:\n  url: http://b" in out


def test_remove_mapping_entry_collapse_keeps_dangling_section_comment():
    """Собственный комментарий записи уходит вместе с ней НАМЕРЕННО, а висячий
    комментарий секции — не её: он описывает секцию и переживает схлопывание,
    ровно как комментарий следующего топ-ключа."""
    text = "models:\n  # реестр заполняется формой\n\n  glm:\n    url: http://a\nbackend:\n  url: http://b\n"
    out = w.remove_mapping_entry(text, "models", "glm")
    assert "# реестр заполняется формой" in out
    assert yaml.safe_load(out)["models"] == {}


def test_remove_mapping_entry_collapse_keeps_parent_trailing_comment():
    """Схлопывание переписывает строку родителя в `parent: {}` — её трейлинг-
    комментарий описывает секцию, а не удаляемую запись."""
    text = 'slots:   # уровни\n  default: "glm"\nbackend:\n  url: x\n'
    out = w.remove_mapping_entry(text, "slots", "default")
    assert "slots: {}   # уровни" in out
    assert yaml.safe_load(out)["slots"] == {}


def test_remove_mapping_entry_missing_entry_is_noop():
    text = "models:\n  glm:\n    url: http://a\n"
    assert w.remove_mapping_entry(text, "models", "flash") == text


def test_remove_mapping_entry_missing_parent_is_noop():
    text = "backend:\n  url: x\n"
    assert w.remove_mapping_entry(text, "models", "flash") == text


# --- ключи с точками (алиасы вида opus-4.8) — смок 2026-07-30 ----------------
# upsert/remove искали запись dotted-путём ("models.opus-4.8" → models→opus-4→8),
# не находили и ДОПИСЫВАЛИ дубль вместо замены (удаление — молчаливый no-op).

_DOTTED_TEXT = (
    "models:\n"
    "  gemini-2.5-flash:\n"
    '    model: "google/gemini-2.5-flash"\n'
    "\n"
    "  opus-4.8:\n"
    '    model: "anthropic/claude-opus-4.8"\n'
    '    url: "https://openrouter.ai/api/v1/chat/completions"\n'
    "slots:\n"
    '  default: "opus-4.8"\n'
)


def test_upsert_replaces_existing_entry_with_dot_in_key():
    out = w.upsert_mapping_entry(
        _DOTTED_TEXT,
        "models",
        "opus-4.8",
        [
            "opus-4.8:",
            '  model: "anthropic/claude-opus-4.8"',
            '  url: "https://other/v1"',
        ],
    )
    assert out.count("  opus-4.8:") == 1  # в models — ровно одна, БЕЗ дубля
    assert '"https://other/v1"' in out
    assert "openrouter.ai" not in out  # старый блок заменён целиком


def test_remove_deletes_entry_with_dot_in_key():
    out = w.remove_mapping_entry(_DOTTED_TEXT, "models", "opus-4.8")
    assert "  opus-4.8:" not in out
    assert "gemini-2.5-flash:" in out  # сосед цел
    assert 'default: "opus-4.8"' in out  # слот (не models) не тронут


def test_upsert_key_named_like_nested_field_not_confused():
    """Алиас «model» не должен матчиться на ПОЛЕ `model:` внутри чужой записи."""
    out = w.upsert_mapping_entry(
        _DOTTED_TEXT, "models", "model", ["model:", '  url: "https://m/v1"']
    )
    # добавилась НОВАЯ запись верхнего уровня, чужие поля model: целы
    assert (
        out.count('    model: "') == 2
    )  # вложенные поля обеих записей не тронуты
    assert "  model:\n" in out  # новая запись-алиас


# --- ключи, которые YAML разбирает НЕ побайтно (llama3:8b, '123', '*') --------
# Писатель квотирует такие ключи (`"llama3:8b":`), а перечитывал регексом,
# режущим строку по ПЕРВОМУ двоеточию: ключ `"llama3` не совпадал ни с чем —
# upsert дописывал ещё один блок (PyYAML берёт последний, файл пух с каждым
# сохранением), remove и переименование молча отказывали.

_COLON_TEXT = 'models:\n  "llama3:8b":\n    url: "http://a"\nslots:\n  default: "llama3:8b"\n'


def test_upsert_replaces_existing_entry_with_colon_in_key():
    out = w.upsert_mapping_entry(
        _COLON_TEXT,
        "models",
        '"llama3:8b"',
        ['"llama3:8b":', '  url: "http://b"'],
    )
    assert out.count('  "llama3:8b":') == 1  # замена, а не второй блок
    assert "http://b" in out and "http://a" not in out


def test_remove_deletes_entry_with_colon_in_key():
    out = w.remove_mapping_entry(_COLON_TEXT, "models", '"llama3:8b"')
    assert '  "llama3:8b":' not in out
    assert 'default: "llama3:8b"' in out  # слот (не models) не тронут


def test_child_lookup_matches_key_by_yaml_value_not_by_quoting_style():
    """Рукописные `'123'` и `'*'` — те же ключи, что писатель кладёт СВОИМИ
    двойными кавычками: сравнение идёт по разобранному скаляру, не по байтам."""
    out = w.remove_mapping_entry(
        "models:\n  '123':\n    url: \"http://a\"\n", "models", '"123"'
    )
    assert yaml.safe_load(out)["models"] == {}
    wildcard = "assign:\n  subagents:\n    '*': \"glm\"\n"
    assert (
        w.remove_mapping_entry(wildcard, "assign.subagents", '"*"') != wildcard
    )


# --- ensure_section_anchor_text / create_profile_file / write_text ------------

_LIVE_SECTION_TEMPLATE = (
    "# реестр моделей\nmodels:\n  flash:\n    url: http://tmpl\n"
)


def test_ensure_section_anchor_reuses_live_template_block():
    """Секция в шаблоне живая (не закомментирована) — вставка реюзает
    `insert_missing` и переносит блок шаблона целиком (с его комментами)."""
    text = "summary:\n  chat_completions_summary_trigger_chars: 1\n"
    out = w.ensure_section_anchor_text(text, "models", _LIVE_SECTION_TEMPLATE)
    assert "models:\n  flash:\n    url: http://tmpl\n" in out
    assert "models: {}" not in out

    # повторный вызов — no-op
    assert (
        w.ensure_section_anchor_text(out, "models", _LIVE_SECTION_TEMPLATE)
        == out
    )


def test_ensure_section_anchor_synthesizes_bare_anchor_for_commented_template(
    tmp_path,
):
    """Закомментированные секции дают пустые якоря (PR140 Р1: `assign` в
    шаблоне теперь тоже закомментирован)."""
    from pathlib import Path

    from gc_core.config import _default_config_path

    template_text = Path(_default_config_path()).read_text(encoding="utf-8")
    once = 'backend:\n  completions_url: "http://x"\n'

    for section in ("models", "slots", "assign"):
        once = w.ensure_section_anchor_text(once, section, template_text)
    parsed = yaml.safe_load(once)
    assert (
        parsed["models"] == {}
        and parsed["slots"] == {}
        and parsed["assign"] == {}
    )

    for section in ("models", "slots", "assign"):
        # повторный вызов — no-op
        assert (
            w.ensure_section_anchor_text(once, section, template_text) == once
        )


def test_create_profile_file_refuses_existing(tmp_path):
    path = tmp_path / "work.yml"
    path.write_text("models: {}\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        w.create_profile_file(path, "models: {}\nslots: {}\n")
    assert path.read_text(encoding="utf-8") == "models: {}\n"


def test_create_profile_file_writes_skeleton(tmp_path):
    path = tmp_path / "nested" / "new_profile.yml"
    w.create_profile_file(path, "models: {}\nslots: {}\n")
    assert path.read_text(encoding="utf-8") == "models: {}\nslots: {}\n"


def test_write_text_writes_atomically(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("models:\n  glm:\n    url: http://a\n", encoding="utf-8")
    new_text = w.upsert_mapping_entry(
        path.read_text(encoding="utf-8"),
        "models",
        "flash",
        ["flash:", "  url: http://b"],
    )
    w.write_text(path, new_text)
    assert path.read_text(encoding="utf-8") == new_text


def test_write_text_validate_failure_leaves_file_untouched(tmp_path):
    path = tmp_path / "config.yml"
    original = "models:\n  glm:\n    url: http://a\n"
    path.write_text(original, encoding="utf-8")

    def _reject(_text):
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        w.write_text(path, "models: broken", validate=_reject)
    assert path.read_text(encoding="utf-8") == original


# ── В3: недостающий топ-уровневый якорь встаёт после соседа по порядку шаблона ─

_ORDERED_TEMPLATE = (
    "# указатель профиля\n"
    'profile: ""\n'
    "# активный уровень\n"
    "level: default\n"
    "\n"
    "# бэкенд\n"
    "backend:\n"
    '  completions_url: "http://sentinel"\n'
)


def test_insert_missing_top_key_lands_after_template_neighbour():
    user = 'profile: "work"\n# бэкенд\nbackend:\n  completions_url: "http://real"\n'
    out = w.insert_missing(user, "level", _ORDERED_TEMPLATE)
    assert out.index("level:") > out.index("profile:")
    assert out.index("level:") < out.index("backend:")
    assert out.index("# бэкенд") < out.index(
        "backend:"
    )  # чужой комментарий не оторван


def test_insert_missing_top_key_appended_when_neighbour_absent():
    user = '# бэкенд\nbackend:\n  completions_url: "http://real"\n'
    out = w.insert_missing(user, "level", _ORDERED_TEMPLATE)
    assert out.index("level:") > out.index(
        "backend:"
    )  # соседа (profile) нет — в конец


def test_ensure_pointer_anchor_level_lands_after_profile(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        'profile: "work"\nbackend:\n  completions_url: "http://real"\n',
        encoding="utf-8",
    )
    template = tmp_path / "config.default.yml"
    template.write_text(_ORDERED_TEMPLATE, encoding="utf-8")

    w.ensure_pointer_anchor(cfg, template, key="level")

    out = cfg.read_text(encoding="utf-8")
    assert out.index("level:") > out.index("profile:")
    assert out.index("level:") < out.index("backend:")


# ── край: файл без завершающего перевода строки (частый итог ручной правки) ───
#
# Фикстуры топ-уровневых случаев — на РЕАЛЬНОМ config.default.yml: блок `level:`
# в нём идёт без прикреплённого комментария, и склейка строк выходит наружу
# как есть. Синтетический шаблон с комментарием над ключом маскирует дефект:
# `profile: "corp"# коммент` PyYAML ещё прощает (кавычка закрыла скаляр).

_HAND_EDITED = (
    'backend:\n  completions_url: "http://real/v1"\nprofile: "corp"'  # без \n
)


def _default_template_text() -> str:
    from pathlib import Path

    from gc_core.config import _default_config_path

    return Path(_default_config_path()).read_text(encoding="utf-8")


def test_uncomment_template_block_reads_the_tight_comment_form_too():
    """Форма без пробела после решётки тоже понимается — шаблоны бывают и такие."""
    tpl = "#gizmo:\n#  provider: openai\n"
    block = w.uncomment_template_block(tpl, "gizmo")
    assert "\ngizmo:\n" in "\n" + block
    assert "provider: openai" in block


# ── §6.4 на НАСТОЯЩЕМ шаблоне: якорь там пишется «# key:», с пробелом ──


def _real_template() -> str:
    from pathlib import Path

    from gc_core.config import _default_config_path

    return Path(_default_config_path()).read_text(encoding="utf-8")


def test_insert_missing_top_key_after_last_key_without_trailing_newline():
    """Сосед — ПОСЛЕДНИЙ ключ файла, файл без финального `\\n`: вставка обязана
    начаться с новой строки, иначе выходит `profile: "corp"level: default`."""
    out = w.insert_missing(_HAND_EDITED, "level", _default_template_text())
    raw = yaml.safe_load(out)
    assert raw["level"] == "default"
    assert raw["profile"] == "corp"


def test_insert_missing_nested_key_into_section_without_trailing_newline():
    """То же на вложенной ветке: конец секции совпал с концом файла без `\\n`."""
    user = 'profile: "corp"\nbackend:\n  use_ssl: false'  # без \n
    out = w.insert_missing(user, "backend.completions_url", _ORDERED_TEMPLATE)
    raw = yaml.safe_load(out)
    assert raw["backend"]["completions_url"] == "http://sentinel"
    assert raw["backend"]["use_ssl"] is False


def test_ensure_pointer_anchor_keeps_file_loadable_without_trailing_newline(
    tmp_path,
):
    """Живой путь `settings.py` (запись уровня рядом с профилем): `ensure_pointer_anchor`
    пишет БЕЗ валидатора — склейку строк тут никто не поймает, и пользовательский
    конфиг остался бы нечитаемым, без шанса на откат."""
    from gc_core.config import _default_config_path

    cfg = tmp_path / "config.yml"
    cfg.write_text(_HAND_EDITED, encoding="utf-8")

    w.ensure_pointer_anchor(cfg, _default_config_path(), key="level")

    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert raw["profile"] == "corp" and raw["level"] == "default"


def test_upsert_mapping_entry_into_section_without_trailing_newline():
    """Тот же шов у записей словаря: конец секции — конец файла без `\\n`."""
    text = "models:\n  glm:\n    url: http://a"  # без \n
    out = w.upsert_mapping_entry(
        text, "models", "flash", ["flash:", "  url: http://b"]
    )
    assert yaml.safe_load(out)["models"] == {
        "glm": {"url": "http://a"},
        "flash": {"url": "http://b"},
    }


def test_write_values_inserts_into_file_without_trailing_newline(tmp_path):
    """Через `write_values` склейку ловит валидатор — но операция, которая раньше
    проходила, не должна начать отказывать."""
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        'profile: "corp"\nbackend:\n  use_ssl: false', encoding="utf-8"
    )
    tpl = tmp_path / "config.default.yml"
    tpl.write_text(_ORDERED_TEMPLATE, encoding="utf-8")

    missing = w.write_values(
        cfg,
        {"backend.completions_url": "http://real/v1"},
        template_path=tpl,
        validate=yaml.safe_load,
    )

    assert missing == []
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["backend"] == {
        "use_ssl": False,
        "completions_url": "http://real/v1",
    }


def test_ensure_section_anchor_bare_anchor_lands_after_template_neighbour():
    """Синтезированный якорь (`models`/`slots`/`assign` в config.default.yml
    закомментированы) размещается по тому же правилу соседа."""
    from pathlib import Path

    from gc_core.config import _default_config_path

    template_text = Path(_default_config_path()).read_text(encoding="utf-8")
    out = 'profile: ""\nlevel: default\nbackend:\n  completions_url: "http://real"\n'

    out = w.ensure_section_anchor_text(out, "models", template_text)
    out = w.ensure_section_anchor_text(out, "slots", template_text)

    assert (
        out.index("level:")
        < out.index("models:")
        < out.index("slots:")
        < out.index("backend:")
    )


def test_uncomment_template_block_finds_the_real_anchor():
    """Живые шаблоны комментируют секции как «# gc:» — с пробелом после решётки.
    Regex требовал «#gc:», блок не находился, и write_values молча уносил ключ в
    missing. Тест выше собирает шаблон сам и проверяет формат, которого в шаблонах
    не бывает, — поэтому дыру не видел."""
    block = w.uncomment_template_block(_real_template(), "gc")
    assert "\ngc:\n" in "\n" + block  # шапка раскомментирована и БЕЗ отступа
    assert (
        "\n  force_collect:" in "\n" + block
    )  # поле — вложенным, как положено YAML


def test_uncomment_template_block_unfolds_a_list_section():
    """Список пресетов — тот же случай: строки блока не «ключ: значение», а элементы."""
    block = w.uncomment_template_block(_real_template(), "endpoint_presets")
    assert "\nendpoint_presets:\n" in "\n" + block
    assert "\n  - name: OpenRouter" in "\n" + block


def test_write_values_unfolds_a_commented_section_and_keeps_yaml_valid(
    tmp_path,
):
    """Сквозной путь: секции в файле нет вовсе → блок разворачивается из шаблона,
    значение пишется, документ остаётся валидным YAML (шапка с ведущим пробелом
    сломала бы разбор)."""
    import yaml
    from gc_core.config import _default_config_path

    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "backend:\n  completions_url: http://h/v1/chat/completions\n",
        encoding="utf-8",
    )
    missing = w.write_values(
        cfg, {"gc.force_collect": "true"}, template_path=_default_config_path()
    )
    assert missing == []
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert (
        "gc" in data and "force_collect" in data["gc"]
    )  # секция развернулась целиком
    assert data["backend"]["completions_url"] == "http://h/v1/chat/completions"


# ── §6.4 на voice-секции: она в шаблоне тоже закомментирована ──


def test_uncomment_voice_block_keeps_the_canonical_indent():
    """Отступ развёрнутого voice-блока — 2 пробела, как во всём файле.

    Разворот снимает решётку И один пробел за ней, поэтому шаблон обязан писать
    закомментированную секцию формой «# voice:» / «#␣␣␣url:». Прежняя форма
    «#voice:» с двумя пробелами давала после разворота отступ 1: YAML при этом
    валиден, поэтому ни один сторож этого не ловил, — а канон файла и последующая
    дописка ключа блоком из шаблона (у него отступ 2) разъезжались."""
    block = w.uncomment_template_block(_real_template(), "voice")
    assert "\nvoice:\n" in "\n" + block  # шапка без отступа
    assert "\n  provider: openai" in "\n" + block  # поле — ровно два пробела


def test_write_voice_values_uncomments_template_block(tmp_path):
    """§6.4: voice-блок шаблона закомментирован — write_values разворачивает его."""
    from gc_core.config import _default_config_path, validate_config_text

    template = Path(_default_config_path())
    cfg = tmp_path / "config.yml"
    cfg.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")

    missing = w.write_values(
        cfg,
        {
            "voice.url": "https://stt.example/v1/audio/transcriptions",
            "voice.token": "sk-x",
            "voice.model": "whisper-1",
        },
        template_path=template,
        validate=validate_config_text,
    )
    assert missing == []
    text = cfg.read_text(encoding="utf-8")
    assert 'url: "https://stt.example/v1/audio/transcriptions"' in text
    assert "provider: openai" in text  # блок развёрнут целиком
    # повторная запись идемпотентна: якоря теперь есть
    assert (
        w.write_values(
            cfg,
            {"voice.model": "whisper-large"},
            template_path=template,
            validate=validate_config_text,
        )
        == []
    )
    assert 'model: "whisper-large"' in cfg.read_text(encoding="utf-8")


# ── Частично живая секция: дописываем в неё, а не сдаёмся ──


def test_write_values_extends_a_partially_live_section(tmp_path):
    """Юзер завёл voice: с одним полем — остальные ДОПИСЫВАЮТСЯ в ту же секцию.

    Находка ревью коллеги: недостающие под-ключи уходили в `missing`, а соседи по
    вызову всё равно попадали в файл. Человек получал половину настроек: /settings
    говорил «не сохранено», мастер — «✓ конфиг записан», а в файле лежало ни то ни сё.

    Второй top-level блок при этом заводить по-прежнему нельзя: PyYAML на дублях не
    ругается и молча берёт последний — юзерский url тихо потерялся бы."""
    import re

    import yaml
    from gc_core.config import _default_config_path, validate_config_text

    template = Path(_default_config_path())
    cfg = tmp_path / "config.yml"
    user_url = "https://my-real-stt.example/v1"
    cfg.write_text(
        template.read_text(encoding="utf-8")
        + f'voice:\n  url: "{user_url}"\n',
        encoding="utf-8",
    )

    missing = w.write_values(
        cfg,
        {"voice.model": "wh", "voice.language": "en"},
        template_path=template,
        validate=validate_config_text,
    )

    assert missing == []
    text = cfg.read_text(encoding="utf-8")
    assert (
        len(re.findall(r"^voice:", text, re.MULTILINE)) == 1
    )  # блок не задублирован
    voice = yaml.safe_load(text)["voice"]
    assert voice["url"] == user_url  # юзерское значение цело
    assert (
        voice["model"] == "wh" and voice["language"] == "en"
    )  # дописанные — на месте


def test_write_values_leaves_the_file_untouched_when_a_key_cannot_be_written(
    tmp_path,
):
    """Записать удалось не всё — файл не трогаем вовсе.

    Половина значений в конфиге хуже честного отказа: человек видит «не сохранено», а
    на диске лежит новый адрес со старой моделью (находка ревью коллеги)."""
    from gc_core.config import _default_config_path

    cfg = tmp_path / "config.yml"
    before = "backend:\n  completions_url: http://h/v1/chat/completions\n"
    cfg.write_text(before, encoding="utf-8")

    missing = w.write_values(
        cfg,
        {
            "backend.completions_url": "http://new/v1/chat/completions",
            "nosuch.key": "x",
        },
        template_path=_default_config_path(),
    )

    assert missing == ["nosuch.key"]
    assert cfg.read_text(encoding="utf-8") == before


def test_remove_mapping_entry_keeps_a_dangling_comment_after_the_last_entry():
    """Комментарий, отделённый от записи пустой строкой, ей не принадлежит —
    удаление записи его не уносит.

    `_exclude_next_attached_comment` защищает только прогон комментариев, вплотную
    примыкающий к следующей содержательной строке И с её отступом. Комментарий с
    отступом секции, висящий после последней записи перед топ-уровневым ключом,
    под это правило не попадал и уезжал в удаляемый диапазон. Валидация потерю не
    ловит — результат схемно валиден, — а бэкапа у записи реестра нет.

    Концом диапазона записи считается конец её последней СОДЕРЖАТЕЛЬНОЙ строки:
    удалять чужой текст безопаснее «недо-», чем «пере-»."""
    text = 'models:\n  a:\n    model: "m-a"\n  b:\n    model: "m-b"\n\n  # ВАЖНЫЙ висячий комментарий\nlevel: "default"\n'

    out = w.remove_mapping_entry(text, "models", "b")

    assert "# ВАЖНЫЙ висячий комментарий" in out
    assert "b:" not in out and "m-b" not in out
    assert 'level: "default"' in out
    assert 'model: "m-a"' in out


def test_remove_mapping_entry_still_drops_the_entrys_own_attached_comment():
    """Сторож от чрезмерной осторожности: комментарий, вплотную примыкающий к
    следующей записи, по-прежнему остаётся ей, а собственный комментарий
    удаляемой записи уходит вместе с ней."""
    text = 'models:\n  # про a\n  a:\n    model: "m-a"\n  # про b\n  b:\n    model: "m-b"\n'

    out = w.remove_mapping_entry(text, "models", "a")

    assert "# про a" not in out  # свой комментарий ушёл с записью
    assert "# про b" in out  # чужой остался при своей записи


def test_upsert_mapping_entry_replace_keeps_a_dangling_comment_after_the_last_entry():
    """Зеркало remove-теста выше для ветки ЗАМЕНЫ (ревью MR #80, M11): комментарий,
    отделённый от записи пустой строкой, ей не принадлежит — замена записи его не
    уносит. Конец диапазона считался голым `_exclude_next_attached_comment(
    section_end(...))` — без `_trim_trailing_noise`, которым тот же расчёт давно
    обёрнут у `remove_mapping_entry`: висячий комментарий хвоста секции уезжал в
    заменяемый диапазон, а с ним и пустая строка-разделитель (повторные прогоны
    слипали файл). Валидация потерю не ловит — результат схемно валиден, — а
    бэкапа у записи реестра нет."""
    text = 'slots:\n  default: "a"\n\n  # light намеренно не задан: см. регламент\nlevel: "default"\n'

    out = w.upsert_mapping_entry(text, "slots", "default", ['default: "b"'])

    assert "# light намеренно не задан" in out
    assert '  default: "b"\n\n' in out  # пустая строка-разделитель цела
    assert 'level: "default"' in out


def test_upsert_mapping_entry_insert_goes_before_a_dangling_comment_not_after():
    """Та же дыра у ветки ВСТАВКИ в непустого родителя: новая запись вставала
    ПОСЛЕ висячего комментария — чужой комментарий становился её шапкой и
    искажал смысл."""
    text = 'models:\n  a:\n    model: "m-a"\n\n  # хвостовой комментарий секции\nlevel: "default"\n'

    out = w.upsert_mapping_entry(text, "models", "b", ["b:", '  model: "m-b"'])

    assert out.index("  b:") < out.index("# хвостовой комментарий секции")
    assert yaml.safe_load(out)["models"] == {
        "a": {"model": "m-a"},
        "b": {"model": "m-b"},
    }
