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
