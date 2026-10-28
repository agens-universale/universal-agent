"""Хранилища на S3: ``S3ThreadStore``, ``S3AgentStore``.

Драйвер — aiobotocore (extra ``gc-stores[s3]``); импорт модуля его не
требует, отказ без драйвера — при первом обращении. Только S3 с условной
записью (If-Match/If-None-Match).
"""

from gc_stores.s3.store import S3AgentStore, S3ThreadStore, open_stores

__all__ = ["S3AgentStore", "S3ThreadStore", "open_stores"]
