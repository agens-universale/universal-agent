import yaml
import os
from uuid import uuid4

import arg_parser
from utils.prod_auth import AuthConnector


custom_config_path = arg_parser.run_args.config
config_dict = dict()
base_dir = os.path.dirname(os.path.abspath(__file__))
config_path = custom_config_path if custom_config_path else os.path.join(base_dir, '..', 'config.yml')

with open(config_path, 'r', encoding='utf-8') as file:
    config_dict = yaml.safe_load(file)

auth_cfg = config_dict.get('agent_auth')

if auth_cfg:
    entry_point=auth_cfg['auth_endpoint']
    env_name=auth_cfg.get('env_name', 'surface_cli')
    env_version=auth_cfg.get('env_version', '0.0.1')
    plugin_name=auth_cfg.get('plugin_name', 'surf-cli-plugin')
    plugin_version=auth_cfg.get('plugin_version', '0.0.1')
    device_code=auth_cfg.get('device_code', str(uuid4()))
    device_code_hex = auth_cfg.get('device_code_hex', None)
    login=auth_cfg.get('login', 'login')
    
    auth_connector = AuthConnector(
        entry_point=entry_point,
        env_name=env_name,
        env_version=env_version,
        plugin_name=plugin_name,
        plugin_version=plugin_version,
        device_code=device_code,
        device_code_hex=device_code_hex,
        login=login
    )
else:
    auth_connector = None

if __name__ == '__main__':
    print(config_dict)
    if auth_connector:
        print(auth_connector.get_token())
