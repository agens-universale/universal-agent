"""Раскладка ключей KV-хранилищ.

Строки извне (система, тред, алиас, имя вложения, агент, проект) — в hex
UTF-8: разделитель «/» или любой символ не ломают раскладку, а порядок
ключей остаётся порядком кодов символов (две hex-цифры на байт, и обе
больше «/»). Индекс списка тредов упорядочен как контракт: новее — раньше,
затем система и тред по возрастанию.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from gc_core.threads.models import ThreadKey

#: Микросекунды от эпохи, «перевёрнутые»: больше время — меньше ключ.
_TICKS = 10**19 - 1
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SCHEMA = "_gc/schema"
INDEX = "idx/"
ALIASES = "alias/"
RECEIPTS = "r/"
THREADS = "t/"


def enc(value: str) -> str:
    return value.encode("utf-8", "surrogatepass").hex()


def dec(value: str) -> str:
    return bytes.fromhex(value).decode("utf-8", "surrogatepass")


def thread_prefix(key: ThreadKey) -> str:
    return f"{THREADS}{enc(key.system)}/{enc(key.thread)}/"


def head_key(key: ThreadKey) -> str:
    return thread_prefix(key) + "h"


def chunk_key(key: ThreadKey, chunk_id: str) -> str:
    return f"{thread_prefix(key)}c/{chunk_id}"


def blob_key(key: ThreadKey, name: str) -> str:
    return f"{thread_prefix(key)}b/{enc(name)}"


def receipt_prefix(key: ThreadKey) -> str:
    return f"{RECEIPTS}{enc(key.system)}/{enc(key.thread)}/"


def receipt_key(key: ThreadKey, incarnation: str, op_id: str) -> str:
    """Квитанция записи ``op_id`` воплощения треда ``incarnation``."""
    digest = hashlib.sha256(op_id.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{receipt_prefix(key)}{incarnation}/{digest}"


def alias_key(alias: str) -> str:
    return ALIASES + enc(alias)


def _ticks(moment: datetime) -> int:
    delta = moment.astimezone(UTC) - _EPOCH
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def index_key(updated_at: datetime, key: ThreadKey) -> str:
    """Ключ индекса: перевёрнутое время, система, тред."""
    inverted = _TICKS - _ticks(updated_at)
    return f"{INDEX}{inverted:019d}/{enc(key.system)}/{enc(key.thread)}"


def index_entry(index: str) -> tuple[datetime, ThreadKey]:
    """Момент и тред из ключа индекса."""
    inverted, system, thread = index[len(INDEX) :].split("/")
    ticks = _TICKS - int(inverted)
    moment = datetime.fromtimestamp(ticks // 1_000_000, UTC).replace(
        microsecond=ticks % 1_000_000
    )
    return moment, ThreadKey(system=dec(system), thread=dec(thread))
