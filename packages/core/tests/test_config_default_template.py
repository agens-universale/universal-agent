"""Стражи config.default.yml:

A1 — значения шаблона не расходятся с кодовыми (pydantic) дефолтами:
     расхождения — только в INTENTIONAL_TEMPLATE_OVERRIDES.
A2 — полнота шаблона: каждый листовой путь схемы либо присутствует в шаблоне,
     либо осознанно занесён в EXPERT_ONLY_KEYS.

Канон дефолтов = AppConfig(). Шаблон — бутстрап/справочник/сентинелы.
"""
from pathlib import Path

import yaml

from gc_core.config import _default_config_path, flatten_app_config
from gc_core.config_modules.model_sections_validation import validate_model_sections
from gc_core.config_modules.root import AppConfig
from gc_testkit.paths import REPO
from gc_core.model_router import ModelRouter

# dotted-пути, где шаблон НАМЕРЕННО отличается от кодового дефолта:
# сентинелы онбординга и preset-значения шаблона. Пополнять только осознанно.
INTENTIONAL_TEMPLATE_OVERRIDES: set = {
    # --- Сентинелы онбординга: placeholder-значения, которые пользователь обязан вписать ---
    "backend.completions_url",                  # "http://your-backend:..." — инструкция вписать URL
    "backend.chat_completion_headers.Authorization",  # "Bearer <ВПИШИТЕ_ТОКЕН>" — инструкция вписать токен
    "agent.model",                              # "<ВПИШИТЕ_МОДЕЛЬ>" — модель выбирается в мастере первого запуска

    # --- Preset-режим шаблона: chat_completions вместо responses (кодового дефолта) ---
    "backend.api_mode",                         # шаблон ориентирован на chat_completions
    "backend.chat_completions_max_tokens",      # 8096 вместо 32768 — разумный лимит для chat_completions
    "backend.use_ssl",                          # false: шаблон нацелен на http://localhost endpoint
    "backend.use_max_tokens_param",             # true: нужен для chat_completions-провайдеров
    "backend.use_retry",                        # true: рекомендованная настройка для продакшн

    # --- Preset-значения агента: рекомендованные настройки для продакшн-использования ---
    "agent.use_local_subagents",               # true: включены subagents в шаблоне
    "agent.subagent_max_steps",                # 500 вместо 100 — больший лимит для продакшн
    "agent.client_name",                       # "gc_ua_agent" — имя клиента для gc-ua
    "agent.no_tool_call_max_count",            # 3 вместо 5 — более строгий лимит прозаических ответов

    # --- Preset TUI ---
    "tui.stream",                              # true (явно), кодовый дефолт None (наследуется от backend.stream)

    # --- Preset суммаризации: включена в шаблоне, выключена в кодовом дефолте ---
    "summary.chat_completions_summary_trigger_chars",  # 200000 вместо 0 (выключено)

    # --- Preset безопасности: SSRF-blacklist и файловые фильтры ---
    "security.url_policy.url_blacklist",        # SSRF-blacklist cloud metadata/loopback/RFC1918
    "security.read_guard.allowed_read_paths",   # whitelist путей навыков
    "security.read_guard.denied_read_paths",    # denylist системных директорий
    "security.read_guard.denied_file_patterns", # защита от чтения .env, id_rsa и т.п.
    "security.read_guard.denied_extensions",    # защита от чтения .key/.pfx/.jks

    "pricing.enabled",                          # true при нулевых ставках ниже
    "pricing.input",                            # 0 — шаблон предлагает вписать реальные ставки
    "pricing.output",                           # 0 — но включённый pricing с нулями даёт startup-notice
}


# Листовые dotted-пути схемы, ОСОЗНАННО отсутствующие в шаблоне.
# Правило: шаблон — минимальный бутстрап для обычного пользователя; эти ключи
# требуют понимания внутренней архитектуры или специфического сценария.
# Полный справочник значений — demo_config.yml.
# Пополнять только осознанно; при появлении ключа в шаблоне — удалять отсюда.
EXPERT_ONLY_KEYS: set = {
    # --- каталог провайдеров: намеренно живёт в коде, а не в шаблоне ---
    # Развёрнутый блок впечатывался бы копией в конфиг каждому при первом запуске,
    # и следующий добавленный провайдер до людей уже не доехал бы. В шаблоне он
    # лежит закомментированным — как образец формата (сторож этого —
    # test_default_presets_are_not_materialized_into_the_template).
    "endpoint_presets",
    # Голосовой каталог — тот же класс и то же правило.
    "voice_endpoint_presets",
    # --- agent: тюнинги finish gate / output storage ---
    # Экспертные лимиты quality-gate цикла; дефолты подходят для большинства сценариев.
    "agent.finish_gate_max_iterations",
    "agent.subagent_finish_gate_max_iterations",
    "agent.finish_gate_use_rule_hints",       # rule-layer без LLM, вне v1
    "agent.tool_output_inline_max_chars",     # лимит инлайн-вывода тула (байты контекста)
    "agent.stored_tool_outputs_max_entries",  # размер стора больших tool-outputs
    "agent.exec_timeout_sec",                # таймаут execute_command / run_python_code
    "agent.walk_output_max_chars",           # потолок приватного канала обхода дерева (Harbor)
    "agent.limit_project_metadata",           # скрыть проектные метаданные из системного промпта
    # Per-run лимиты надёжности (по умолчанию выключены); см. docs/configuration.md.
    "agent.session_token_limit",
    "agent.max_tool_calls",
    # Списки по умолчанию пусты; наполнение — проектный оверлей (.gc/config.yml)
    "agent.disable_skills",
    # Синтетические поля tool schema — инжект description к параметру «reason»;
    # задаётся под конкретную модель / промпт.
    "agent.inject_tool_fields",
    # Имя HTTP-заголовка для внешнего trace_id; None по умолчанию, нужен только
    # при внешней трассировке (напр. интеграция с оркестратором заказчика).
    # Промпт для кастомных сабагентов: дефолт резолвится из bundled prompts/,
    # переопределяется только при своей редакции промпта. См. docs/subagents.md.
    "paths.chat_completions_custom_subagent_system_prompt_path",
    "agent.trace_id_header_name",
    # Разрешение слать trace_id в заголовке к модели; False по умолчанию —
    # рассылка трейса наружу opt-in, нужна только при внешней трассировке.
    "agent.trace_header_enabled",
    # Кастомные сабагенты из папки: None по умолчанию (только встроенные классы),
    # лимит имеет смысл только вместе с папкой. См. docs/subagents.md.
    "agent.subagents_dir",
    "agent.subagents_max_count",
    # Model steering → special_instructions; строка специфичных инструкций для модели.
    "agent.model_steering",

    # --- agent_auth: корпоративная сертификатная авторизация ---
    # Секция целиком — корп-сценарий mTLS; wiring через runbook, не для публичного шаблона.
    "agent_auth",

    # --- backend: параметры chat_completions ---
    # Vendor-specific kwargs (temperature, top_p, …); живёт в backend.chat_completion_kwargs.
    "backend.chat_completion_kwargs",
    # Минимум токенов в ответе (провайдеро-специфичный параметр).
    "backend.chat_completions_min_response_tokens",
    # Ретрай ошибки, зашитой прокси в HTTP-200 (choices[0].error / top-level
    # error / finish_reason=error); дефолт true, тюнинг — под конкретный прокси.
    "backend.retry_embedded_choice_errors",
    # Глобальный флаг стриминга; управляется через tui.stream (в шаблоне есть).
    "backend.stream",
    # Закомментированный пример; дефолт — авто по id модели.
    "backend.claude_prompt_cache",

    # --- chat_completions: вспомогательный LLM (summary / finish gate / intent scope) ---
    # Тюнинг вспомогательных вызовов; публичный шаблон не нацелен на chat_completions.
    "chat_completions.llm.max_tokens",
    "chat_completions.llm.completion_kwargs",
    "chat_completions.llm.judge_max_tokens",  # запас под CoT судей; секции нет в шаблоне
    "chat_completions.llm.subagent_role_max_tokens",  # лимит генерации роли special_agent
    # --- backend mTLS: корп-сценарий, справочник — demo_config.yml ---
    "backend.cert_path",
    "backend.key_path",
    # --- тюнинги, пришедшие с master: место справочника — demo_config.yml ---
    "agent.max_unknown_tool_repeats",         # порог повторов неизвестного тула
    "tui.bang_shell.timeout_sec",             # таймаут !shell-команды (None = без лимита)
    "tui.file_view.page_lines",               # шаг PgUp/PgDn в /view (None = авто по высоте)
    "logging.redact_secrets.enabled",         # редакция секретов в per-run логах
    "logging.redact_secrets.extra_keys",
    "logging.redact_secrets.extra_patterns",

    # --- intent_scope: детальный тюнинг intent-log ---
    # Секция целиком доступна только при backend.api_mode: chat_completions.
    # max_final_answer_raw_chars — в шаблоне есть; остальное — экспертный тюнинг.
    "intent_scope.max_summary_chars",
    "intent_scope.max_tool_output_chars",
    "intent_scope.max_raw_record_chars",
    "intent_scope.read_page_record_limit",
    "intent_scope.fallback_raw_on_llm_error",
    # Пути к кастомным prompt'ам вспомогательного LLM.
    "intent_scope.intent_scope_tool_summary_prompt_path",
    "intent_scope.intent_scope_final_answer_summary_prompt_path",
    # Конфигурация per-tool — типовой пример в demo_config.yml.
    "intent_scope.tools",

    # --- mcp: wiring серверов и таймауты ---
    # Серверы задаются через gc-mcp install / project-оверлей, не в публичном шаблоне.
    "mcp.servers",
    # Таймауты SDK; дефолты приемлемы, тюнинг нужен только при нестандартных сетевых условиях.
    "mcp.meta.tools_cache_ttl",
    "mcp.meta.request_timeout_sec",
    "mcp.meta.discovery_timeout_sec",
    # Путь к манифесту рекомендованных MCP; управляется gc-mcp.
    "mcp.meta.recommended_mcp_manifest",

    # --- voice: голосовой ввод (опциональная секция) ---
    # В шаблоне — осознанно закомментированный пример (voice выключен по умолчанию);
    # заполняется юзером вручную или целиком пишется gc-voice install в config.local.yml.
    "voice.provider",
    "voice.url",
    "voice.token",
    "voice.model",
    "voice.language",
    "voice.max_seconds",
    "voice.timeout_seconds",
    "voice.verify_ssl",

    # --- paths: кастомные пути к промптам и runtime-данным ---
    # По умолчанию все пути берутся из XDG/GC_HOME; явное задание — только для sandbox.
    "paths.threads_data_path",
    "paths.logs_dump_dir",
    "paths.skills_data_path",
    # Пути к кастомным system-prompt файлам; типовой пример закомментирован в demo.
    "paths.chat_completions_system_prompt_path",
    "paths.chat_completions_intent_guard_prompt_path",
    "paths.chat_completions_special_instructions_path",

    # --- pricing: биллинг токенов ---
    # Базовые ключи (enabled/currency/per_tokens/input/output) шаблон несёт сам —
    # они пришли из master; здесь остаётся только тюнинг сверх него.
    "pricing.cache_read",
    "pricing.models",  # пустой dict — leaf в schema_dump

    # --- project_memory: long memory (проектная память) ---
    # Доступна только при наличии project-memory файла; дефолты работают без настройки.
    "project_memory.enable_project_memory_notice",
    "project_memory.check_interval_messages",
    "project_memory.max_chars",
    "project_memory.max_append_chars",
    "project_memory.compaction_max_tokens",

    # --- quality_gate: лимиты digest для finish gate ---
    # Экспертный тюнинг; дефолты подходят для большинства сценариев.
    "quality_gate.max_dialogue_chars",
    "quality_gate.max_message_chars",

    # --- security.download: настройки тула download_file ---
    # Каталог загрузки и лимит размера; обычно менять не нужно.
    "security.download.download_dir",
    "security.download.max_bytes",

    # --- security.read_guard: forward-compat поля бинарного чтения ---
    # В v1 не enforce'ятся (см. комментарий в security.py); тюнинг на будущее.
    "security.read_guard.binary_read_policy",
    "security.read_guard.binary_allowed_types",
    "security.read_guard.binary_allowed_extensions",
    "security.read_guard.max_file_size_kb",

    # --- summary: тюнинг суммаризации ---
    # Доступна только при backend.api_mode: chat_completions.
    # chat_completions_summary_trigger_chars — в шаблоне есть (preset включён).
    # Остальные — экспертный тюнинг параметров компакции.
    "summary.chat_completions_summary_prompt_path",
    "summary.chat_completions_memory_patch_replace_prompt_path",
    "summary.chat_completions_summary_turn_end_trigger_chars",
    "summary.incremental_batch_chars",
    "summary.intent_log_preview_chars",

    # --- tool_usage_notice: технические reminder'ы о неиспользованных тулах ---
    # Список правил; типовой пример в demo_config.yml. Пусто = фича выключена.
    "tool_usage_notice",

    # --- working_memory: лимиты регистров session_memory ---
    # Тюнинг char-бюджета рабочей памяти агента; дефолты работают без настройки.
    "working_memory.max_chars_goal",
    "working_memory.max_chars_constraints",
    "working_memory.max_chars_state",
    "working_memory.max_chars_knowledge",
    "working_memory.total_budget_chars",

    # --- Профили v2 (волна A): секции models/slots/assign ---
    # Схема готова (Task 1); полный документированный пример в шаблоне — Task 15.
    # Указатели profile/level уже в шаблоне (совпадают с кодовыми дефолтами).
    "models",
    "slots.light",
    "slots.default",
    "slots.max",
    "assign.finish_gate",
    "assign.text_answer_judge",
    "assign.command_judge",
    "assign.summary",
    "assign.intent_scope",
    "assign.memory_compaction",
    "assign.auto_judge",
    "assign.subagents",
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
    """§14 «Стражи»: шаблон с живым якорем `profile: ""` остаётся ЛЕГАСИ.

    Второй рубеж, а не единственный: ожившую в шаблоне секцию моделей ловят и
    A1, и A2 выше (`models`, `slots.*`, `assign.*` лежат в `EXPERT_ONLY_KEYS`,
    и появление их в шаблоне даёт `stale` — проверено раскомментированием).
    Ценность здесь в другом: те двое падают на бухгалтерии ключей и говорят
    «whitelist разошёлся с шаблоном», а требование §14 — про ПОВЕДЕНИЕ. Этот
    формулирует его прямо и спрашивает Router: пакетная поставка обязана
    резолвиться плоскими ключами, как до профилей, иначе уровни включились бы
    всем, кто ещё ничего не настраивал.
    """
    raw = _template_raw()
    assert raw["profile"] == ""
    assert raw["level"] == "default"

    router = ModelRouter(
        flatten_app_config(AppConfig.model_validate(raw)),
        level_provider=lambda: raw["level"],
    )
    assert router.registry_empty() is True
    assert router.resolve("main").source == "легаси"


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
        'demo_config.yml потерял живой указатель `profile: ""` — §13 требует '
        "демонстрации заполнения указателей"
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
    пример на месте, живым он НЕ становится (иначе demo перестал бы показывать
    легаси-умолчание), а все три секции — из тех, что демонстрирует §13.
    """
    text = DEMO_CONFIG_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    for anchor in ("# models:", "# slots:", "# assign:"):
        assert any(line.startswith(anchor) for line in lines), (
            f"demo_config.yml потерял закомментированный якорь {anchor!r} — "
            "§13 требует пример секций моделей как обычных конфиг-секций"
        )
    raw = yaml.safe_load(text) or {}
    for section in ("models", "slots", "assign"):
        assert section not in raw, (
            f"секция {section} в demo_config.yml ожила — пример обязан оставаться "
            "закомментированным (§13: демо показывает и легаси-умолчание тоже)"
        )


def test_demo_config_commented_example_is_valid_when_uncommented():
    """Пример существует, чтобы его раскомментировали, — значит он обязан
    грузиться. Проверяется тем же путём, что и правка руками: блок снимается с
    комментария, кладётся поверх остального demo_config и идёт через обе фазы
    (схема + ссылочная целостность на merged), а не только через `safe_load`."""
    text = DEMO_CONFIG_PATH.read_text(encoding="utf-8")
    example = yaml.safe_load(_uncomment_block(text, "# models:")) or {}
    assert set(example) == {"models", "slots", "assign"}, (
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
        if sp in template_paths or any(tp.startswith(sp + ".") for tp in template_paths):
            covered.add(sp)
    return covered


def test_schema_keys_present_in_template_or_expert_only():
    """A2: каждый листовой путь схемы либо есть в шаблоне, либо в EXPERT_ONLY_KEYS.

    Три ассерта (точная симметрия):
    - missing  : новый ключ схемы не попал ни в шаблон, ни в whitelist → дрейф.
    - stale    : ключ из whitelist появился в шаблоне → запись устарела.
    - phantom  : ключ в whitelist не существует в схеме → мёртвая запись.
    """
    schema_paths = _leaf_paths(AppConfig().model_dump())
    template_paths = _leaf_paths(_template_raw())
    covered = _template_covered(schema_paths, template_paths)

    missing = schema_paths - covered - EXPERT_ONLY_KEYS
    stale = EXPERT_ONLY_KEYS & covered
    phantom = EXPERT_ONLY_KEYS - schema_paths

    assert not missing, (
        "Новые ключи схемы отсутствуют в шаблоне — добавь в config.default.yml "
        "ИЛИ осознанно в EXPERT_ONLY_KEYS:\n" + "\n".join(sorted(missing))
    )
    assert not stale, (
        "EXPERT_ONLY_KEYS содержит ключи, которые теперь есть в шаблоне — удали из whitelist:\n"
        + "\n".join(sorted(stale))
    )
    assert not phantom, (
        "EXPERT_ONLY_KEYS содержит пути, отсутствующие в схеме (AppConfig) — удали мёртвые записи:\n"
        + "\n".join(sorted(phantom))
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
    demo_file = REPO / name          # demo_config.yml лежит в корне репозитория
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
