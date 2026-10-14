"""Транзакция файлового хранилища тредов v2 поверх журнала ``.store/``.

Формат ``pending.json`` тот же, что у журнала develop
(``gc_core.thread_store_journal``): изменения файлов (полный образ или
дописывание с ``offset``), новый ``control.json`` и квитанция. Поэтому
прерванную запись доигрывает тот же ``_recover`` — при следующем входе в
``thread_read_scope`` любого писателя, старого или нового.

Режим надёжности:

* ``atomic`` — журнал без fsync: запись переживает падение процесса (данные
  уже в кэше ОС), но не отключение питания. Так писал develop.
* ``fsync`` — каждый файл и каталог сбрасываются на диск, как в журнале
  durable-хранилища develop.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from gc_core.thread_store_journal import (
    _json,
    _recover,
    _write,
)

Durability = Literal["atomic", "fsync"]

PENDING = "pending.json"
CONTROL = "control.json"
RECEIPTS = "receipts"


def receipt_path(base: Path, op_id: str) -> Path:
    name = hashlib.sha256(op_id.encode()).hexdigest() + ".json"
    return base / RECEIPTS / name


def digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


class Changes:
    """Изменения файлов одной транзакции — в формате ``pending.json``."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.items: list[dict[str, Any]] = []

    def _relative(self, path: str | Path) -> str:
        return str(Path(path).relative_to(self.directory))

    def put(self, path: str | Path, after: bytes | None) -> None:
        """Полный образ файла; ``None`` — удалить."""
        current = Path(path)
        before = current.read_bytes() if current.exists() else None
        if before == after:
            return
        self.items.append(
            {
                "path": self._relative(path),
                "before": digest(before),
                "after": None
                if after is None
                else base64.b64encode(after).decode(),
            }
        )

    def append(self, path: str | Path, tail: bytes) -> None:
        """Дописать в конец; размер до записи — ``offset`` для повтора."""
        if not tail:
            return
        current = Path(path)
        offset = current.stat().st_size if current.exists() else 0
        self.items.append(
            {
                "path": self._relative(path),
                "offset": offset,
                "append": base64.b64encode(tail).decode(),
            }
        )


def _plain_write(path: Path, data: bytes) -> None:
    from gc_core.utils.atomic_io import atomic_replace

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-v2")
    temporary.write_bytes(data)
    atomic_replace(str(temporary), str(path))


def _apply_plain(directory: Path, change: dict[str, Any]) -> None:
    path = directory / change["path"]
    if "offset" in change:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as stream:
            stream.write(base64.b64decode(change["append"]))
        return
    if change["after"] is None:
        path.unlink(missing_ok=True)
        return
    _plain_write(path, base64.b64decode(change["after"]))


def write_control(
    base: Path, control: dict[str, Any], durability: Durability
) -> None:
    """Только ``control.json`` (аренда): один файл, атомарная замена."""
    if durability == "fsync":
        _write(base / CONTROL, _json(control))
    else:
        _plain_write(base / CONTROL, _json(control))


def publish(
    directory: Path,
    base: Path,
    *,
    changes: Changes,
    control: dict[str, Any],
    receipt: dict[str, Any],
    identity: dict[str, str],
    durability: Durability,
) -> None:
    """Записать журнал транзакции и применить его.

    Вызывается под ``thread_read_scope`` треда; ``receipt`` содержит
    ``operation_id`` — по нему журнал кладёт квитанцию.
    """
    journal = {
        "changes": sorted(changes.items, key=lambda c: "offset" not in c),
        "identity": identity,
        "control": control,
        "receipt": receipt,
    }
    if durability == "fsync":
        _write(base / PENDING, _json(journal))
        _recover(directory, base)
        return
    _plain_write(base / PENDING, _json(journal))
    for change in journal["changes"]:
        _apply_plain(directory, change)
    _plain_write(base / CONTROL, _json(control))
    _plain_write(receipt_path(base, receipt["operation_id"]), _json(receipt))
    (base / PENDING).unlink()


def read_receipt(base: Path, op_id: str) -> dict[str, Any] | None:
    path = receipt_path(base, op_id)
    if not path.exists():
        return None
    return json.loads(path.read_bytes())
