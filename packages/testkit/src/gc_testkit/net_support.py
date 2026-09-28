"""Сетевые отказы в той форме, в какой их отдаёт httpx.

Ядро и голос ходят в сеть httpx, и TLS-провал приходит НЕ как «ошибка TLS»:
верхний тип — `httpx.ConnectError`, а настоящий `ssl.SSLError` лежит в цепочке
причин. Проверено на самоподписанном сертификате, цепочка была
``httpx.ConnectError -> httpcore.ConnectError -> ssl.SSLCertVerificationError``.

Помощник здесь, а не в тестах: его просят два набора (voice_runtime и voice), а
общий код тестов в этом репозитории живёт в gc_testkit — пакетов `tests` с
`__init__.py` тут нет, и импорт «из соседнего теста» не резолвится.
"""

from __future__ import annotations

import ssl

import httpx


def tls_failure(
    message: str = "self-signed certificate in chain",
) -> httpx.ConnectError:
    """TLS-отказ так, как его видит вызывающий httpx: причина — в цепочке."""
    err = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]")
    err.__cause__ = ssl.SSLCertVerificationError(message)
    return err
