import yaml
import os

import arg_parser


custom_config_path = arg_parser.run_args.config
config_dict = dict()
base_dir = os.path.dirname(os.path.abspath(__file__))
config_path = custom_config_path if custom_config_path else os.path.join(base_dir, '..', 'config.yml')

with open(config_path, 'r', encoding='utf-8') as file:
    config_dict = yaml.safe_load(file)

if config_dict.get('system_prompt') is None:
    config_dict['system_prompt'] = dict()

if __name__ == '__main__':
    print(config_dict)

