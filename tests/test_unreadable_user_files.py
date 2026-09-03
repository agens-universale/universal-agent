"""Класс «файл человека в чужой кодировке»: чтение объясняется, а не роняет процесс.

`UnicodeDecodeError` — подкласс `ValueError`, а НЕ `OSError`. Любой `except OSError:`
вокруг чтения текстового файла его пропускает. Человек сохраняет конфиг блокнотом
Windows в UTF-16 — приложение падает трейсбеком там, куда он и пришёл чинить.

Класс объявляли закрытым четырьмя коммитами подряд, и каждый раз следующую щель
владелец находил живьём: закрывали ПЕРЕЧЕНЬ мест, а не класс. Поэтому сторож здесь
параметризован по классу целиком — каждый читатель файла человека получает
НАСТОЯЩИЙ файл в UTF-16 во временной папке (не мок: подделка кодировки моком и
проверяла бы подделку) и обязан либо деградировать, либо отказать ошибкой, которая
НАЗЫВАЕТ файл. Голая жалоба кодека без пути человеку бесполезна: он не знает, какой
из файлов чинить.

Новый читатель файла человека добавляется СЮДА строкой `_Case`, а не отдельным
тестом рядом: отдельными тестами класс и закрывали четыре раза.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Осмысленный YAML/JSON/markdown — именно в UTF-16: файл ЗАКОННЫЙ по содержанию и
# негодный только кодировкой. Пустышка вроде b"\xff\xfe" прошла бы мимо тех
# читателей, что отваливаются раньше декодирования.
_BODY_YAML = "model: gpt-4\nmodels:\n  fast:\n    model: m1\n"
_BODY_JSON = '{"pid": 1, "started_at": "2026-01-01T00:00:00+00:00"}'
_BODY_TEXT = "Ты полезный ассистент.\nОтвечай кратко.\n"


def _write_utf16(path: Path, body: str) -> Path:
    """Файл, сохранённый «блокнотом» Windows: UTF-16 LE с BOM."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode("utf-16"))
    return path


@dataclass(frozen=True)
class _Case:
    """Одна функция класса: что ей подсунуть и как её позвать.

    `call(path)` зовёт функцию по НАСТОЯЩЕМУ пути к файлу в UTF-16. `setup(tmp)`
    (необязательный) готовит окружение и возвращает kwargs для `call`.
    """

    id: str
    filename: str
    body: str
    call: Callable[..., Any]
    setup: Callable[..., dict] | None = None
    marks: tuple = field(default_factory=tuple)


def _cases() -> list[_Case]:
    cases: list[_Case] = []

    # --- слои конфига (дома правила, закрытые прежними коммитами) ---
    def _read_document(path, **_):
        import config_raw
        return config_raw.read_document(path)

    def _safe_yaml_dict(path, **_):
        from config_modules import layers
        return layers._safe_yaml_dict(path)

    def _readable_layer_document(path, **_):
        from config_modules import layers
        return layers._readable_layer_document(path, "config.yml")

    def _load_profile_document(path, **_):
        from config_modules.profiles import load_profile_document
        return load_profile_document(path)

    def _registry_read(path, **_):
        from config_modules import registry_write
        return registry_write._read(path)

    def _instructions_read(path, **_):
        from instructions import loader
        return loader._read(Path(path))

    cases += [
        _Case("config_raw.read_document", "config.yml", _BODY_YAML, _read_document),
        _Case("layers._safe_yaml_dict", "config.yml", _BODY_YAML, _safe_yaml_dict),
        _Case("layers._readable_layer_document", "config.yml", _BODY_YAML,
              _readable_layer_document),
        _Case("profiles.load_profile_document", "work.yml", _BODY_YAML,
              _load_profile_document),
        _Case("registry_write._read", "config.yml", _BODY_YAML, _registry_read),
        _Case("instructions.loader._read", "AGENTS.md", _BODY_TEXT, _instructions_read),
    ]

    # --- синхронизация конфига по пакетному шаблону ---
    def _sync_config(path, **_):
        import config_sync
        return config_sync.sync_config(path)

    def _reset_config_template(path, *, target, **_):
        import config_sync
        return config_sync.reset_config(target, template_path=path)

    def _reset_setup(tmp: Path) -> dict:
        target = tmp / "config.yml"
        target.write_text(_BODY_YAML, encoding="utf-8")
        return {"target": target}

    cases += [
        _Case("config_sync.sync_config", "config.yml", _BODY_YAML, _sync_config),
        _Case("config_sync.reset_config(шаблон)", "config.default.yml", _BODY_YAML,
              _reset_config_template, setup=_reset_setup),
    ]

    # --- писатель конфига (отказ обязан назвать файл) ---
    def _write_values(path, **_):
        import onboarding_config_writer as ocw
        return ocw.write_values(path, {"model": "m"})

    def _ensure_pointer_anchor(path, *, template, **_):
        import onboarding_config_writer as ocw
        return ocw.ensure_pointer_anchor(path, template, key="profile")

    def _ensure_section_anchor(path, **_):
        import onboarding_config_writer as ocw
        return ocw.ensure_section_anchor(path, "models", "models: {}\n")

    def _template_setup(tmp: Path) -> dict:
        template = tmp / "config.default.yml"
        template.write_text("profile: \"\"\nmodel: m\n", encoding="utf-8")
        return {"template": template}

    cases += [
        _Case("ocw.write_values", "config.yml", _BODY_YAML, _write_values),
        _Case("ocw.ensure_pointer_anchor", "config.yml", _BODY_YAML,
              _ensure_pointer_anchor, setup=_template_setup),
        _Case("ocw.ensure_section_anchor", "config.yml", _BODY_YAML,
              _ensure_section_anchor),
    ]

    # --- память проекта: эти файлы человек правит руками чаще прочих ---
    def _memory_read(path, **_):
        from project_memory import storage
        return storage.read(Path(path))

    def _memory_stat(path, **_):
        from project_memory import storage
        return storage.stat(Path(path))

    def _memory_append(path, **_):
        from project_memory import storage
        return storage.append(Path(path), "новая заметка")

    cases += [
        _Case("project_memory.storage.read", "memory.md", _BODY_TEXT, _memory_read),
        _Case("project_memory.storage.stat", "memory.md", _BODY_TEXT, _memory_stat),
        _Case("project_memory.storage.append", "memory.md", _BODY_TEXT, _memory_append),
    ]

    # --- история ввода TUI ---
    def _input_history_load(path, **_):
        from tui.services.input_history import InputHistory
        history = InputHistory(Path(path).parent)
        history.load()
        return history.size

    cases.append(
        _Case("InputHistory.load", "input_history.jsonl", _BODY_JSON,
              _input_history_load))

    # --- экраны TUI: чтение идёт на КАЖДОМ проходе отрисовки меню ---
    def _target_snapshot(path, **_):
        from tui.screens.settings import SettingsScreen
        screen = SettingsScreen.__new__(SettingsScreen)
        screen._config_path = str(path)
        screen._menu_pass_snapshot = None
        screen._parsed_target = None
        return screen._target_snapshot()

    def _file_snapshot(path, **_):
        from tui.screens.settings import SettingsScreen
        screen = SettingsScreen.__new__(SettingsScreen)
        return screen._file_snapshot(path)

    def _restore_snapshot(path, **_):
        from tui.screens.settings import SettingsScreen
        screen = SettingsScreen.__new__(SettingsScreen)
        return screen._restore_snapshot(path, "model: m\n")

    def _post_update_needs_sync(path, **_):
        import config_sync
        from tui.screens.post_update import PostUpdateScreen
        screen = PostUpdateScreen.__new__(PostUpdateScreen)
        screen._config_path = str(path)
        screen._config_sync = config_sync
        return screen._compute_needs_sync()

    def _read_lock(path, **_):
        from tui.services import session_lock
        return session_lock.read_lock(str(path))

    cases += [
        _Case("SettingsScreen._target_snapshot", "config.yml", _BODY_YAML,
              _target_snapshot, marks=("textual",)),
        _Case("SettingsScreen._file_snapshot", "config.yml", _BODY_YAML,
              _file_snapshot, marks=("textual",)),
        _Case("SettingsScreen._restore_snapshot", "config.yml", _BODY_YAML,
              _restore_snapshot, marks=("textual",)),
        _Case("PostUpdateScreen._compute_needs_sync", "config.yml", _BODY_YAML,
              _post_update_needs_sync, marks=("textual",)),
        _Case("session_lock.read_lock", "session.lock", _BODY_JSON, _read_lock),
    ]

    # --- конфиги вне основного стека ---
    def _server_config(path, *, monkeypatch, **_):
        from config_modules import env_vars
        from config_modules.env_vars import EnvVars
        import rest_api.server_config as server_config
        monkeypatch.setattr(env_vars, "env", EnvVars(server_config_path=Path(path)))
        return server_config.load_server_config()

    def _voice_base_config(path, *, monkeypatch, **_):
        import config as config_mod
        from voice_runtime import wire
        monkeypatch.setattr(config_mod, "resolve_config_path", lambda _p: str(path))
        return wire.base_config_raw()

    def _load_managed(path, *, monkeypatch, **_):
        from mcp_runtime import paths as mcp_paths
        from mcp_runtime import wiring
        monkeypatch.setattr(mcp_paths, "managed_config_path", lambda: Path(path))
        return wiring.load_managed()

    def _read_snippet(path, **_):
        from mcp_runtime import wiring
        return wiring.read_snippet(Path(path).parent)

    cases += [
        _Case("server_config.load_server_config", "config_server.yml", _BODY_YAML,
              _server_config),
        _Case("voice_runtime.wire.base_config_raw", "config.yml", _BODY_YAML,
              _voice_base_config),
        _Case("mcp_runtime.wiring.load_managed", "config.local.yml", _BODY_YAML,
              _load_managed),
        _Case("mcp_runtime.wiring.read_snippet", "agent_config.snippet.yml", _BODY_YAML,
              _read_snippet),
    ]

    # --- определения кастомных сабагентов: файлы пишет человек ---
    def _discover_subagents(path, **_):
        from tools.subagents.custom.file_definitions import discover_subagent_definitions
        return discover_subagent_definitions(
            {"subagents_dir": str(Path(path).parent)}, builtin_names=())

    cases.append(
        _Case("file_definitions.discover_subagent_definitions", "reviewer.md",
              "---\nname: reviewer\n---\nТы ревьюер.\n", _discover_subagents))

    # --- промпты по путям из конфига: человек вправе показать на свой файл ---
    def _summary_prompt(path, **_):
        from summarize import prompt_loading
        return prompt_loading.load_prompt_template(str(path))

    def _intent_scope_prompt(path, **_):
        from intent_scope import summarizer
        return summarizer._load_prompt_file(str(path))

    def _intent_guard_prompt(path, **_):
        from intent_guard import final_report_consistency
        return final_report_consistency._load_guard_prompt(str(path))

    def _command_judge_prompt(path, **_):
        from security import command_moderation
        return command_moderation._load_prompt_file(Path(path))

    cases += [
        _Case("summarize.load_prompt_template", "summary_prompt.md", _BODY_TEXT,
              _summary_prompt),
        _Case("intent_scope._load_prompt_file", "scope_prompt.md", _BODY_TEXT,
              _intent_scope_prompt),
        _Case("intent_guard._load_guard_prompt", "guard_prompt.md", _BODY_TEXT,
              _intent_guard_prompt),
        _Case("command_moderation._load_prompt_file", "judge_prompt.md", _BODY_TEXT,
              _command_judge_prompt),
    ]

    return cases


CASES = _cases()


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_utf16_user_file_does_not_crash(case: _Case, tmp_path, monkeypatch):
    """Функция класса, получив НАСТОЯЩИЙ файл в UTF-16, не выпускает
    `UnicodeDecodeError` наружу; если она отказывает — отказ называет файл."""
    if "textual" in case.marks:
        pytest.importorskip("textual")
    path = _write_utf16(tmp_path / case.filename, case.body)
    kwargs = case.setup(tmp_path) if case.setup is not None else {}
    try:
        case.call(path, monkeypatch=monkeypatch, **kwargs)
    except UnicodeDecodeError as exc:      # именно тот класс, ради которого сторож
        pytest.fail(
            f"{case.id}: UnicodeDecodeError ушёл наружу — приложение упадёт "
            f"трейсбеком на файле человека: {exc}"
        )
    except Exception as exc:               # отказ законен, безымянный отказ — нет
        assert path.name in str(exc), (
            f"{case.id}: отказ не называет файл ({type(exc).__name__}: {exc}) — "
            f"человеку нечего чинить"
        )
