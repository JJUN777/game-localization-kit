import os
import json
import logging

# 프로젝트의 루트 디렉토리를 이 파일(common.py)의 위치로 설정
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def setup_logging():
    """로그 설정을 초기화합니다. 로그는 콘솔에만 출력됩니다."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler()
        ]
    )


def load_config(config_filename="00_config.json"):
    """설정 파일을 로드합니다."""
    config_path = os.path.join(PROJECT_ROOT, config_filename)
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logging.info(f"Config loaded from {config_path}")
        return config
    except FileNotFoundError:
        logging.error(f"FATAL: Configuration file not found at {config_path}")
        return None
    except json.JSONDecodeError:
        logging.error(f"FATAL: Could not decode {config_filename}. Check for JSON syntax errors.")
        return None


def get_absolute_path(relative_path):
    """프로젝트 루트를 기준으로 상대 경로의 절대 경로를 반환합니다."""
    return os.path.join(PROJECT_ROOT, relative_path)


def get_api_key(config):
    """설정에서 API 키를 직접 읽어옵니다."""
    if not config or 'translation' not in config:
        logging.error("API key could not be read: 'translation' section is missing in config.")
        return None

    api_key = config['translation'].get('api_key_env_var')
    if not api_key:
        logging.error("API key could not be read: 'api_key_env_var' is missing in config.")
        return None

    return api_key