"""
Утилиты для извлечения SSL/TLS параметров из конфига MCP-сервера.
"""
import logging
import os
from typing import Any, Dict, Optional, Tuple, Union

logger = logging.getLogger(__name__)


_FALSE_TOKENS = ("false", "0", "no", "off")
_TRUE_TOKENS = ("true", "1", "yes", "on")


def get_verify_ssl(server_cfg: Dict[str, Any]) -> Union[bool, str]:
    """
    Определить режим проверки SSL-сертификата по конфигу сервера.

    Поддерживает:
      verify-ssl: true/false                       — стандартная проверка / отключена
      verify-ssl: "true"/"false"/"0"/"1"/"no"/...  — строковые алиасы bool
      verify-ssl: "/path/to/ca-bundle.pem"         — путь к CA-bundle (`~` и env-vars
                                                    раскрываются); пробрасывается в
                                                    requests.verify / httpx.verify как есть.
    """
    verify_cfg = (server_cfg or {}).get("verify-ssl")

    if isinstance(verify_cfg, bool):
        return verify_cfg
    if isinstance(verify_cfg, str):
        token = verify_cfg.strip()
        if token.lower() in _FALSE_TOKENS:
            return False
        if token.lower() in _TRUE_TOKENS:
            return True
        # Считаем строку путём к CA-bundle.
        return _expand_path(token)
    return True


def get_client_cert(server_cfg: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """
    Извлечь клиентский сертификат и ключ из конфига сервера.

    Если заданы оба ключа client-cert и client-key — возвращает кортеж (cert, key)
    с раскрытыми `~` и переменными окружения.
    Если задан только один — логирует предупреждение и возвращает None.
    Если ни один не задан — возвращает None.
    """
    cfg = server_cfg or {}
    cert_path = cfg.get("client-cert")
    key_path = cfg.get("client-key")

    if cert_path and key_path:
        return (_expand_path(str(cert_path)), _expand_path(str(key_path)))

    if cert_path or key_path:
        logger.warning(
            "mTLS: задан только %s без %s — клиентский сертификат не будет использован",
            "client-cert" if cert_path else "client-key",
            "client-key" if cert_path else "client-cert",
        )
        return None

    return None


def _expand_path(path: str) -> str:
    """Раскрыть `~` и переменные окружения в пути к серту/CA-bundle."""
    return os.path.expanduser(os.path.expandvars(path))
