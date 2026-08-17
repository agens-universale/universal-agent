"""InputHistory — per-project история input-реплик (JSONL).

Файл: <project_dir>/input_history.jsonl
Формат строки: {"ts": "<iso-utc>", "text": "<полный текст реплики>"}

Поведение:
- append дедуплицирует adjacent (не дублируем последнюю запись).
- При превышении MAX_ENTRIES каждые REWRITE_EVERY append'ов файл
  переписывается с tail-MAX_ENTRIES (rolling FIFO).
- Битые JSON-строки при load — пропускаются, не валят процесс.

Навигация через prev/next (Task 2).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from gc_core.utils.atomic_io import atomic_replace


def _is_command(text: str) -> bool:
    """Слэш-команда (`/init`, `/status`, …) — не пользовательский запрос.
    Команды НЕ участвуют в истории ↑/↓: иначе их восстановление в инпут
    открывает палитру, та перехватывает ↑/↓ и навигация по истории «застревает»."""
    return text.lstrip().startswith("/")


def _is_command(text: str) -> bool:
    """Слэш-команда (`/init`, `/status`, …) — не пользовательский запрос.
    Команды НЕ участвуют в истории ↑/↓: иначе их восстановление в инпут
    открывает палитру, та перехватывает ↑/↓ и навигация по истории «застревает»."""
    return text.lstrip().startswith("/")


class InputHistory:
    """Per-project история input-реплик с навигацией ↑/↓."""

    MAX_ENTRIES = 1000
    REWRITE_EVERY = 50

    HISTORY_FILENAME = "input_history.jsonl"

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = Path(project_dir)
        self._file = self._project_dir / self.HISTORY_FILENAME
        self._entries: list[str] = []
        self._appends_since_rewrite: int = 0

    @property
    def size(self) -> int:
        return len(self._entries)

    def load(self) -> None:
        self._entries = []
        if not self._file.exists():
            return
        try:
            content = self._file.read_text(encoding="utf-8")
        except OSError:
            return
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = entry.get("text")
            if isinstance(text, str) and not _is_command(text):
                self._entries.append(text)   # старые command-записи отсеиваем

    def append(self, text: str) -> None:
        if not text or _is_command(text):
            return
        if self._entries and self._entries[-1] == text:
            return
        self._entries.append(text)
        try:
            self._project_dir.mkdir(parents=True, exist_ok=True)
            with self._file.open("a", encoding="utf-8") as f:
                ts = datetime.now(timezone.utc).isoformat()
                f.write(json.dumps({"ts": ts, "text": text}, ensure_ascii=False) + "\n")
        except OSError:
            pass

        self._appends_since_rewrite += 1
        if (
            len(self._entries) > self.MAX_ENTRIES
            and self._appends_since_rewrite >= self.REWRITE_EVERY
        ):
            self._rewrite_tail()

    def _rewrite_tail(self) -> None:
        self._entries = self._entries[-self.MAX_ENTRIES:]
        try:
            self._project_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_suffix(self._file.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                ts = datetime.now(timezone.utc).isoformat()
                for text in self._entries:
                    f.write(json.dumps({"ts": ts, "text": text}, ensure_ascii=False) + "\n")
            atomic_replace(tmp, self._file)
            self._appends_since_rewrite = 0
        except OSError:
            pass

    def prev(self, current_index: int | None) -> tuple[int, str] | None:
        """Шаг назад по истории. None если двигаться некуда.

        current_index=None → начать с конца (вернуть последнюю запись).
        current_index=size → вирт. позиция «один за последним», вернуть последнюю.
        """
        if not self._entries:
            return None
        if current_index is None:
            new_index = len(self._entries) - 1
        else:
            new_index = current_index - 1
        if new_index < 0:
            return None
        if new_index >= len(self._entries):
            new_index = len(self._entries) - 1
        return new_index, self._entries[new_index]

    def next(self, current_index: int | None) -> tuple[int, str] | None:
        """Шаг вперёд по истории. None если за пределами размера."""
        if not self._entries or current_index is None:
            return None
        new_index = current_index + 1
        if new_index >= len(self._entries):
            return None
        return new_index, self._entries[new_index]
