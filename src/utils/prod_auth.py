import requests
import time
from typing import Optional
import hashlib


class AuthConnector:

    def __init__(self, entry_point: str, env_name: str, env_version: str, plugin_name: str,
                 plugin_version: str, device_code: str, device_code_hex: str, login: str, verify: bool):
        self.auth_url = None
        self.ttl = 0
        self.access_token = None
        self.verify=verify

        # auth metadata
        self.entry_point = entry_point
        self.env_name = env_name
        self.env_version = env_version
        self.plugin_name = plugin_name
        self.plugin_version = plugin_version
        self.device_code = device_code
        self.device_code_hex = device_code_hex if device_code_hex else hashlib.sha256(self.device_code.encode('utf-8')).hexdigest()
        self.login = login


    @staticmethod
    def get_auth_url(url, verify) -> Optional[str]:
        resp = requests.get(url, timeout=10, verify=verify)
        if resp.status_code != 401:
            raise Exception(f'Incorrect status code from entry point: {resp.status_code}')

        auth_url = resp.headers.get('X-Authorization-Url', None)
        if auth_url is None:
            raise Exception('Auth url is incorrect. Check entry point')
        return auth_url

    @staticmethod
    def create_token(auth_url, env_name: str, env_version: str, plugin_name: str,
                     plugin_version: str, device_code: str, login: str, verify: bool):
        """
        curl '<url>' \
        --header 'Content-Type: application/json' \
        --data '{
            "env_name": "vscode",
            "env_version": "1.106.3",
            "plugin_name": "cli-plugin",
            "plugin_version": "26.2.1",
            "device_code": "fbee4f3e67d54d2f29c77acfc2718ee67926f851f9357191b5bfbb8f44b4d699",
            "login": "login"
            }'

        Response: # Запрос вернет Access Token и время жизни токена в секунда
        {
          "access_token":"eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyX2lkIjoiNT...",
          "expires_in":600
        }
        """
        data = {
            'env_name': env_name,
            'env_version': env_version,
            'plugin_name': plugin_name,
            'plugin_version': plugin_version,
            'device_code': device_code,
            'login': login,
            'locale': 'ru'
        }
        # print(data)
        # print(auth_url)
        resp = requests.post(auth_url, json=data, timeout=10, verify=verify)
        # print(resp)
        resp_data = resp.json()
        return resp_data

    def get_token(self) -> str:
        now = time.time()
        if now < self.ttl and self.access_token is not None:
            return self.access_token

        auth_url = self.get_auth_url(self.entry_point, self.verify)

        self.auth_url = auth_url

        resp_data = self.create_token(self.auth_url, self.env_name, self.env_version, self.plugin_name,
                                      self.plugin_version, self.device_code_hex, self.login, self.verify)
        # print(resp_data)

        self.access_token = resp_data['access_token']
        self.ttl = time.time() + int(resp_data['expires_in']) - 20

        return self.access_token
