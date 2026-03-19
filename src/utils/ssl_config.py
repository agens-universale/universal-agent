"""
Утилиты для извлечения SSL/TLS параметров из конфига MCP-сервера.
"""
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def get_verify_ssl(server_cfg: Dict[str, Any]) -> bool:
    """
    Определить режим проверки SSL-сертификата по конфигу сервера.

    Поддерживает:
      verify-ssl: true/false | "true"/"false"/"0"/"1"/"no"/"off"
    """
    verify_cfg = (server_cfg or {}).get("verify-ssl")
    if isinstance(verify_cfg, bool):
        return verify_cfg
    if isinstance(verify_cfg, str):
        return verify_cfg.strip().lower() not in ("false", "0", "no", "off")
    return True


def get_client_cert(server_cfg: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """
    Извлечь клиентский сертификат и ключ из конфига сервера.

    Если заданы оба ключа client-cert и client-key — возвращает кортеж (cert, key).
    Если задан только один — логирует предупреждение и возвращает None.
    Если ни один не задан — возвращает None.
    """
    cfg = server_cfg or {}
    cert_path = cfg.get("client-cert")
    key_path = cfg.get("client-key")

    if cert_path and key_path:
        return (str(cert_path), str(key_path))

    if cert_path or key_path:
        logger.warning(
            "mTLS: задан только %s без %s — клиентский сертификат не будет использован",
            "client-cert" if cert_path else "client-key",
            "client-key" if cert_path else "client-cert",
        )
        return None

    return None
