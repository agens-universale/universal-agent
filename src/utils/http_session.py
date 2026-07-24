"""Общая requests.Session для backend/auth/probe: trust_env=False.

Вынесено из connect_backend, чтобы prod_auth/model_picker не создавали
циклический импорт connect_backend → config_modules → auth → prod_auth.
"""

from __future__ import annotations

import requests

# Session без чтения ~/.netrc: requests при trust_env=True читает ~/.netrc,
# и Basic-auth оттуда перетирает явный Authorization-заголовок → 401.
# trust_env=False также отключает env-прокси и REQUESTS_CA_BUNDLE — CA передаём
# явно через verify=. Прокси — только через resolve_proxies + proxies= на запросе.
# Без cookies/auth — общую сессию безопасно звать из разных потоков.
NO_NETRC_SESSION = requests.Session()
NO_NETRC_SESSION.trust_env = False
