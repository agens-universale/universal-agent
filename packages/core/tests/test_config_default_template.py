"""Стражи config.default.yml:

A1 — значения шаблона не расходятся с кодовыми (pydantic) дефолтами:
     расхождения — только в INTENTIONAL_TEMPLATE_OVERRIDES.
A2 — полнота шаблона: каждый листовой путь схемы либо присутствует в шаблоне,
     либо осознанно помечен экспертным (``gc_meta(expert=True)`` в модели).

Канон дефолтов = AppConfig(). Шаблон — бутстрап/справочник/сентинелы.
"""

from pathlib import Path

import yaml
from gc_core.config import _default_config_path, load_settings
from gc_core.config_modules.field_meta import meta_for
from gc_core.config_modules.model_sections_validation import (
    validate_model_sections,
)
from gc_core.config_modules.root import AppConfig
from gc_core.startup_notices import collect_startup_notices
from gc_core.model_router import ModelRouter
from gc_testkit.paths import REPO

# dotted-пути, где шаблон НАМЕРЕННО отличается от кодового дефолта:
# сентинелы онбординга и preset-значения шаблона. Пополнять только осознанно.
INTENTIONAL_TEMPLATE_OVERRIDES: set = {
    # --- Сентинелы онбординга: placeholder-значения, которые пользователь обязан вписать ---
    "backend.completions_url",  # "http://your-backend:..." — инструкция вписать URL
    "backend.chat_completion_headers.Authorization",  # "Bearer <ВПИШИТЕ_ТОКЕН>" — инструкция вписать токен
    "agent.model",  # "<ВПИШИТЕ_МОДЕЛЬ>" — модель выбирается в мастере первого запуска
    # --- Preset-режим шаблона: chat_completions вместо responses (кодового дефолта) ---
    "backend.api_mode",  # шаблон ориентирован на chat_completions
    "backend.chat_completions_max_tokens",  # 8096 вместо 32768 — разумный лимит для chat_completions
    "backend.use_ssl",  # false: шаблон нацелен на http://localhost endpoint
    "backend.use_max_tokens_param",  # true: нужен для chat_completions-провайдеров
    "backend.use_retry",  # true: рекомендованная настройка для продакшн
    # --- Preset-значения агента: рекомендованные настройки для продакшн-использования ---
    "agent.use_local_subagents",  # true: включены subagents в шаблоне
    "agent.subagent_max_steps",  # 500 вместо 100 — больший лимит для продакшн
    "agent.client_name",  # "gc_ua_agent" — имя клиента для gc-ua
    "agent.no_tool_call_max_count",  # 3 вместо 5 — более строгий лимит прозаических ответов
    # --- Preset TUI ---
    "tui.stream",  # true (явно), кодовый дефолт None (наследуется от backend.stream)
    # --- Preset суммаризации: включена в шаблоне, выключена в кодовом дефолте ---
    "summary.chat_completions_summary_trigger_chars",  # 200000 вместо 0 (выключено)
    # Шаблон вырезает мысль: копирование reasoning_content даёт 400 у части бэкендов.
    # Кодовый дефолт (ключа нет) — false, поле копируется.
    "strip_tags_projection.strip_think",
    # --- Preset безопасности: SSRF-blacklist и файловые фильтры ---
    "security.url_policy.url_blacklist",  # SSRF-blacklist cloud metadata/loopback/RFC1918
    "security.read_guard.allowed_read_paths",  # whitelist путей навыков
    "security.read_guard.denied_read_paths",  # denylist системных директорий
    "security.read_guard.denied_file_patterns",  # защита от чтения .env, id_rsa и т.п.
    "security.read_guard.denied_extensions",  # защита от чтения .key/.pfx/.jks
    "pricing.enabled",  # true при нулевых ставках ниже
    "pricing.input",  # 0 — шаблон предлагает вписать реальные ставки
    "pricing.output",  # 0 — но включённый pricing с нулями даёт startup-notice
}


def _expert_only_keys() -> set:
    """Листовые dotted-пути схемы, ОСОЗНАННО отсутствующие в шаблоне.

    Правило: шаблон — минимальный бутстрап для обычного пользователя; эти ключи
    требуют понимания внутренней архитектуры или специфического сценария.
    Полный справочник значений — demo_config.yml.

    Список живёт в моделях: поле помечено ``gc_meta(expert=True)`` (флаг секции
    наследуют её листья — ``agent_auth``, ``mcp.servers``, ``models``...).
    Здесь он только сводится к гранулярности дампа схемы: путь листа
    ``AppConfig().model_dump()`` (пустой dict/list и None — листья)."""
    return {
        path
        for path in _leaf_paths(AppConfig().model_dump())
        if meta_for(path).expert
    }


def _diff_paths(a, b, prefix=""):
    """Dotted-пути листьев, где a != b (обход по ключам a|b)."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in set(a) | set(b):
            p = f"{prefix}.{key}" if prefix else str(key)
            out.extend(_diff_paths(a.get(key), b.get(key), p))
        return out
    if a != b:
        out.append(prefix)
    return out


def _template_raw() -> dict:
    text = Path(_default_config_path()).read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def test_default_template_matches_code_defaults():
    validated = AppConfig.model_validate(_template_raw()).model_dump()
    defaults = AppConfig().model_dump()
    diff = set(_diff_paths(validated, defaults))
    extra = sorted(diff - INTENTIONAL_TEMPLATE_OVERRIDES)
    stale = sorted(INTENTIONAL_TEMPLATE_OVERRIDES - diff)
    assert diff == INTENTIONAL_TEMPLATE_OVERRIDES, (
        "config.default.yml разошёлся с pydantic-дефолтами.\n"
        f"Новые расхождения (проверь: сентинел/preset или дрейф?): {extra}\n"
        f"Устаревшие записи whitelist: {stale}"
    )


# Якорь — корень репозитория из gc_testkit.paths, а не `parents[1]`: у develop
# тест лежал в корневом tests/, у нас — в packages/core/tests, и тот же счёт
# вёл бы в packages/core.
DEMO_CONFIG_PATH = REPO / "demo_config.yml"


def test_default_template_with_live_profile_anchor_stays_legacy():
    """Пустой реестр шаблона сохраняет вызов базовой модели.

    Привязки потребителей уже заданы, но без реестра они не включают уровни
    пользователю, который ещё не настроил models и slots.
    """
    raw = _template_raw()
    assert raw["profile"] == ""
    assert raw["level"] == "default"

    router = ModelRouter(
        AppConfig.model_validate(raw),
        choice_provider=lambda: raw["level"],
    )
    assert router.registry_empty() is True
    assert router.resolve("main").source == "легаси"


def test_shipped_templates_carry_no_live_assign():
    """Уровни служебных потребителей без привязки живут в коде (`unbound_level`,
    спека PR140 Р1): шаблоны их не навязывают, поэтому и предупреждения
    «привязки без реестра» у поставляемых файлов нет."""
    for path in (Path(_default_config_path()), DEMO_CONFIG_PATH):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        assert "assign" not in raw, f"{path.name}: активный assign вернулся"
        loaded = load_settings(str(path))
        notices = collect_startup_notices(
            loaded,
            set(),
            None,
            is_tui=False,
            compat_notices=loaded.runtime.notices,
        )
        assert not any(
            "привязки без реестра моделей не действуют" in n.message
            for n in notices
        )


def test_demo_config_validates_against_schema():
    raw = yaml.safe_load(DEMO_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    AppConfig.model_validate(raw)


def test_demo_config_carries_live_profile_and_level_pointers():
    """§13: demo_config.yml демонстрирует заполнение указателей `profile`/`level`.

    В отличие от секций моделей (страж ниже) указатели в демо ЖИВЫЕ — это их
    штатная форма записи, и демо показывает именно её. Стража на них не было:
    оба ключа удалялись из demo_config, не роняя ни одного теста из 133 —
    соседний `test_demo_config_validates_against_schema` требует лишь, чтобы
    файл проходил схему, а схема оба ключа считает опциональными.
    """
    raw = yaml.safe_load(DEMO_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    assert raw.get("profile") == "", (
        'demo_config.yml потерял живой указатель `profile: ""` — §13 требует демонстрации заполнения указателей'
    )
    assert raw.get("level") == "default", (
        "demo_config.yml потерял живой указатель `level: default`"
    )


def _anchor_line(lines: list[str], anchor: str) -> int:
    """Индекс строки-якоря блока. Якорь опознаётся по началу строки: у секций есть
    хвостовой комментарий-пояснение (`# slots:   # уровни — УКАЗАТЕЛИ ...`)."""
    return next(i for i, line in enumerate(lines) if line.startswith(anchor))


def _uncomment_block(text: str, anchor: str) -> str:
    """Блок закомментированного YAML от строки `anchor` до первой не-комментарной
    строки, со снятым `# `. Пустая строка-разделитель внутри блока — сам `#`."""
    lines = text.splitlines()
    start = _anchor_line(lines, anchor)
    out = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        out.append(line[2:] if line.startswith("# ") else line[1:])
    return "\n".join(out)


def test_demo_config_carries_commented_model_sections_example():
    """§13/§14: demo_config.yml несёт ЗАКОММЕНТИРОВАННЫЙ пример секций моделей.

    `test_demo_config_validates_against_schema` рядом этого не ловит: комментарии
    для `yaml.safe_load` невидимы, и он одинаково зелен, если блок удалить и если
    его раскомментировать. Здесь проверяется ровно то, чего он не видит:
    пример `models`/`slots` на месте, живым он НЕ становится (иначе demo перестал
    бы показывать легаси-умолчание); `assign` — только закомментированный пример:
    уровни без привязки задаёт код.
    """
    text = DEMO_CONFIG_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    for anchor in ("# models:", "# slots:"):
        assert any(line.startswith(anchor) for line in lines), (
            f"demo_config.yml потерял закомментированный якорь {anchor!r} — "
            "§13 требует пример секций моделей как обычных конфиг-секций"
        )
    raw = yaml.safe_load(text) or {}
    for section in ("models", "slots"):
        assert section not in raw, (
            f"секция {section} в demo_config.yml ожила — пример обязан оставаться "
            "закомментированным (§13: демо показывает и легаси-умолчание тоже)"
        )
    assert "assign" not in raw, "demo_config.yml снова навязывает привязки"


def test_demo_config_commented_example_is_valid_when_uncommented():
    """Пример существует, чтобы его раскомментировали, — значит он обязан
    грузиться. Проверяется тем же путём, что и правка руками: блок снимается с
    комментария, кладётся поверх остального demo_config и идёт через обе фазы
    (схема + ссылочная целостность на merged), а не только через `safe_load`.
    """
    text = DEMO_CONFIG_PATH.read_text(encoding="utf-8")
    example = yaml.safe_load(_uncomment_block(text, "# models:")) or {}
    assert set(example) == {"models", "slots"}, (
        f"из блока-примера вынулись не те секции: {sorted(example)}"
    )
    merged = (yaml.safe_load(text) or {}) | example
    cfg = AppConfig.model_validate(merged)
    assert validate_model_sections(cfg) == []


def _leaf_paths(obj, prefix: str = "") -> set:
    """Dotted-пути листьев obj. Пустой dict/list и None — листья."""
    out: set = set()
    if isinstance(obj, dict):
        if not obj:
            out.add(prefix)
        else:
            for k, v in obj.items():
                p = f"{prefix}.{k}" if prefix else str(k)
                out.update(_leaf_paths(v, p))
    else:
        out.add(prefix)
    return out


def _template_covered(schema_paths: set, template_paths: set) -> set:
    """Пути схемы, покрытые шаблоном напрямую или как префикс (для пустых dict-листьев)."""
    covered: set = set()
    for sp in schema_paths:
        if sp in template_paths or any(
            tp.startswith(sp + ".") for tp in template_paths
        ):
            covered.add(sp)
    return covered


def test_schema_keys_present_in_template_or_expert_only():
    """A2: каждый листовой путь схемы либо есть в шаблоне, либо помечен
    экспертным в модели (``gc_meta(expert=True)``).

    Два ассерта:
    - missing  : новый ключ схемы не попал ни в шаблон, ни в экспертные → дрейф.
    - stale    : экспертный ключ появился в шаблоне → флаг устарел.
    Мёртвых записей больше не бывает: флаг стоит на самом поле схемы.
    """
    schema_paths = _leaf_paths(AppConfig().model_dump())
    template_paths = _leaf_paths(_template_raw())
    covered = _template_covered(schema_paths, template_paths)
    expert_only = _expert_only_keys()

    missing = schema_paths - covered - expert_only
    stale = expert_only & covered

    assert not missing, (
        "Новые ключи схемы отсутствуют в шаблоне — добавь в config.default.yml "
        "ИЛИ осознанно пометь поле gc_meta(expert=True):\n"
        + "\n".join(sorted(missing))
    )
    assert not stale, (
        "Экспертные ключи (gc_meta(expert=True)) теперь есть в шаблоне — сними флаг с поля:\n"
        + "\n".join(sorted(stale))
    )


# Ключи, которых нет в demo-файлах ещё до появления этого стража. Долг, а не
# разрешение: список должен только сокращаться. Новый дрейф сюда не дописывать —
# правило проекта требует синхронной правки demo_config.yml и config.default.yml.
DEMO_KNOWN_GAPS: set = {
    "agent.limit_trace_summary",
    "backend.chat_completion_headers",
    "pricing.input",
    "pricing.output",
    "tui.bang_shell.enabled",
    "tui.bang_shell.max_output_bytes",
    "tui.bang_shell.max_output_lines",
}


_RUNTIME_PAYLOAD_KEYS = frozenset(
    {"model", "messages", "tools", "max_tokens", "max_completion_tokens"}
)


def test_shipped_template_kwargs_are_null_and_strip_think_stays_on():
    """Поставка: оба глобальных словаря явно null (не {}), strip_think включён."""
    raw = _template_raw()
    assert raw["backend"]["chat_completion_kwargs"] is None
    assert (
        raw["chat_completions"]["auxiliary_llm"]["completion_kwargs"] is None
    )
    assert raw["strip_tags_projection"]["strip_think"] is True


def test_demo_config_has_no_live_vendor_kwargs():
    """Разобранный demo_config не несёт vendor-kwargs и непустых main/auxiliary."""
    raw = yaml.safe_load(DEMO_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    assert raw["backend"]["chat_completion_kwargs"] is None
    assert (
        raw["chat_completions"]["auxiliary_llm"]["completion_kwargs"] is None
    )
    for alias, entry in (raw.get("models") or {}).items():
        if not isinstance(entry, dict):
            continue
        for section in ("main", "auxiliary"):
            assert not entry.get(section), (
                f"models.{alias}.{section} в demo_config.yml непустой"
            )


def test_default_template_payload_has_only_runtime_keys():
    """Старт из шаблона: в тело запроса не попадает ни один vendor-ключ."""
    from types import SimpleNamespace

    from gc_core.config_modules.model_registry import (
        effective_completion_kwargs,
    )
    from gc_core.utils.connect_backend import create_call_data_completions

    cfg = AppConfig.model_validate(_template_raw())
    assert cfg.backend.chat_completion_kwargs == {}
    state = SimpleNamespace(messages=[], tools=[], default_model="m")
    data = create_call_data_completions(
        [{"role": "user", "content": "hi"}],
        state=state,
        settings=cfg,
    )
    assert set(data) <= _RUNTIME_PAYLOAD_KEYS
    assert "max_tokens" in data or "max_completion_tokens" in data
    assert effective_completion_kwargs(None, cfg, section="auxiliary") == {}
    assert effective_completion_kwargs(None, cfg, section="main") == {}


def test_demo_config_covers_the_same_keys_as_the_template():
    """demo_config.yml объявлен полным справочником — значит и полнота та же.

    Страж A2 читал только config.default.yml, поэтому 13 ключей
    fs_tools.find_in_files доехали в шаблон и не доехали в demo: расхождение
    прошло мимо CI. Правило проекта требует синхронной правки обоих файлов.
    """
    schema_paths = _leaf_paths(AppConfig().model_dump())
    template_paths = _leaf_paths(_template_raw())
    template_covered = _template_covered(schema_paths, template_paths)

    # Полным справочником объявлен именно demo_config.yml (config.default.yml:5,
    # docs/configuration.md). demo_config_openai.yml — вариант под провайдера,
    # полноты с него не требуем.
    name = "demo_config.yml"
    demo_file = REPO / name  # demo_config.yml лежит в корне репозитория
    demo_raw = yaml.safe_load(demo_file.read_text(encoding="utf-8")) or {}
    demo_covered = _template_covered(schema_paths, _leaf_paths(demo_raw))

    missing = template_covered - demo_covered - DEMO_KNOWN_GAPS
    assert not missing, (
        f"{name} отстал от config.default.yml — добавь ключи:\n"
        + "\n".join(sorted(missing))
    )
    closed = DEMO_KNOWN_GAPS & demo_covered
    assert not closed, (
        f"{name}: эти ключи уже добавлены — удали их из DEMO_KNOWN_GAPS:\n"
        + "\n".join(sorted(closed))
    )


def test_shipped_templates_carry_only_product_auto_routing_keys():
    defaults = AppConfig().agent
    for raw in (
        _template_raw(),
        yaml.safe_load(DEMO_CONFIG_PATH.read_text(encoding="utf-8")),
    ):
        agent = raw["agent"]
        assert agent["auto_routing"] is defaults.auto_routing
        assert (
            agent["auto_routing_safe_level"]
            == defaults.auto_routing_safe_level
        )
        assert (
            agent["auto_routing_two_level"]
            == defaults.auto_routing_two_level.model_dump()
        )
        assert {key for key in agent if key.startswith("auto_routing")} == {
            "auto_routing",
            "auto_routing_safe_level",
            "auto_routing_two_level",
        }
