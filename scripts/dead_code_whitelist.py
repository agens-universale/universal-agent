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

# Необязательные возможности файлового хранилища (записи итога хода и смены
# модели): SyncThreadStoreAdapter берёт их по имени (`_optional_append(name)` →
# getattr), сессия — свойство порта тоже по имени. gc_core/thread_store_async.py.
_.append_turn_outcome
_.append_model_choice

# Хук AsyncThreadStoreBase — пара к ThreadStoreBase.ensure_ready; парность методов двух
# баз сторожит packages/core/tests/test_thread_store_one_contract.py.
_.aensure_ready

# Абстрактный метод harbor.agents.base.BaseAgent.setup: зовёт Harbor перед запуском агента.
# integrations/harbor/adapters/gc_cli_external.py.
_.setup
