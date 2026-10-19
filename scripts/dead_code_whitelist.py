"""Белый список для scripts/dead_code_check.py (формат vulture).

Имена, которые живы по причине, невидимой статическому анализу: вызов по
строке, реэкспорт для внешних пользователей пакета, контракт колбэка с
обязательной сигнатурой. Каждая строка — с пояснением, почему это не мёртвый
код. Файл никто не импортирует: vulture только читает из него «использования».
"""

# Вызывается по имени: parse_json_object_argument(error_handler_method="_json_arg_error")
# зовёт getattr(self, error_handler_method) — packages/core/src/gc_core/tools/skills/skills_tools.py.
_._json_arg_error

# Контракт колбэка PortAudio (sounddevice): callback(indata, frames, time_info, status) —
# сигнатуру задаёт драйвер, аргумент нельзя убрать. packages/tui/src/gc_tui/voice/recorder.py.
time_info

# Заглушка readability.Document.summary(html_partial=...) в gc_testkit: повторяет
# сигнатуру настоящей библиотеки, чтобы вызовы с этим аргументом не падали.
html_partial

# Native sandbox eagerly loads the pure finish-gate module before replay starts;
# the common guard later accesses it through its ordinary lazy import.
_final_report_consistency

# Переопределение argparse.ArgumentParser._print_message: argparse зовёт его сам
# (print_help, print_usage) — вывод справки в потоки команды config.
# packages/core/src/gc_core/config_command.py.
_._print_message

# Контракт колбэка PortAudio (sounddevice): callback(indata, frames, time_info, status) —
# сигнатуру задаёт драйвер, аргумент нельзя убрать. packages/tui/src/gc_tui/voice/recorder.py.
frames

# Переопределение Textual Widget.get_content_height(container, viewport, width): сигнатуру
# задаёт фреймворк, аргумент нельзя убрать. packages/tui/src/gc_tui/widgets/user_reply.py.
container

# Протокол рантайма для публичного AsyncAgent.create(durable=...): реализует
# gc_core_temporalio.TemporalDurability; ядро его типом не ссылается. gc_core/aio/durable.py.
DurableRuntime

# Публичный контракт AsyncThreadStore/AsyncThreadStoreBase (docs/sdk/thread-store-contract.md,
# docs/async.md): ядро их не зовёт, но встраивающий реализует и вызывает. Покрывает и
# SyncThreadStoreAdapter, и прокси gc_core_temporalio.thread_store_proxy.
_.aload_messages
_.adiscard_message

# Хук AsyncThreadStoreBase — пара к ThreadStoreBase.ensure_ready; парность методов двух
# баз сторожит packages/core/tests/test_thread_store_one_contract.py.
_.aensure_ready

# Абстрактный метод harbor.agents.base.BaseAgent.setup: зовёт Harbor перед запуском агента.
# integrations/harbor/adapters/gc_cli_external.py.
_.setup

# Публичный API PostgresThreadStore (docs/sdk/thread-store.md, «Postgres»): таблицы
# создаёт встраивающий при старте, ядро хранилище не создаёт и не зовёт.
# packages/stores/src/gc_stores/postgres/store.py.
_.ensure_schema

# Публичный API gc_testkit для авторов хранилищ: засеять Seed конформанса через
# запись ресурсов (ResourceWriter хранилищ gc-stores). Зовут тесты gc-stores.
# packages/testkit/src/gc_testkit/agent_store_conformance.py.
seed_store

# Интерфейс записи ресурсов хранилищ агента gc-stores (ResourceWriter): его зовут
# gc-stores sync и встраивающие, которые кладут промпты и скиллы в базу сами.
# packages/stores/src/gc_stores/admin.py, sql/agent_store.py.
ResourceWriter
_.delete_resource
_.list_resources

# KV-движок gc-stores: MemoryKV — KV в памяти для проверки движка (тесты, свои
# реализации встраивающих), sweep — уборка сирот прерванных записей, которую
# зовёт оператор. packages/stores/src/gc_stores/kv/.
MemoryKV
_.sweep
