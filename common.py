import os
import json
import time
import logging

import google.genai as genai

# 프로젝트의 루트 디렉토리를 이 파일(common.py)의 위치로 설정
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 기본 이미지 확장자 (config에 없을 때 fallback)
DEFAULT_IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp')

# 기본 설정 파일명
DEFAULT_CONFIG_FILENAME = "00_config.json"


def setup_logging(log_filename=None):
    """Initialize logging for console and optional file output."""
    handlers = [logging.StreamHandler()]

    if log_filename:
        log_path = os.path.join(PROJECT_ROOT, log_filename)
        handlers.append(logging.FileHandler(log_path, encoding='utf-8'))

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers,
        force=True
    )


def load_config(config_filename=DEFAULT_CONFIG_FILENAME):
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

    api_key = config['translation'].get('api_key')
    if not api_key:
        logging.error("API key could not be read: 'api_key' is missing in config.")
        return None

    return api_key


def build_prompt_string(raw_prompt):
    """리스트 또는 문자열 형태의 프롬프트를 단일 문자열로 변환합니다."""
    if isinstance(raw_prompt, list):
        return "\n".join(raw_prompt)
    return raw_prompt or ""


def get_image_extensions(config):
    """config에서 지원 이미지 확장자 목록을 튜플로 반환합니다."""
    exts = config.get('supported_image_extensions', None)
    if exts and isinstance(exts, list):
        return tuple(e.lower() for e in exts)
    return DEFAULT_IMAGE_EXTENSIONS


def list_image_files(directory, config):
    """디렉토리에서 지원되는 이미지 파일 목록을 반환합니다."""
    exts = get_image_extensions(config)
    return [f for f in os.listdir(directory) if f.lower().endswith(exts)]


# 재시도하지 않을 영구적 HTTP 오류 상태 코드
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404}


def _is_retryable_error(error):
    """재시도 가능한 오류인지 판별합니다. 인증/잘못된 요청 등 영구적 오류는 False."""
    error_str = str(error).lower()
    # google-genai SDK는 상태 코드를 메시지에 포함시킴
    for code in _NON_RETRYABLE_STATUS_CODES:
        if str(code) in error_str:
            return False
    # 인증/권한 관련 키워드
    for keyword in ('invalid api key', 'permission denied', 'not found', 'invalid argument'):
        if keyword in error_str:
            return False
    return True


def call_gemini_with_retry(client, model_name, contents, max_retries=3, base_delay=2):
    """
    Gemini API 호출을 exponential backoff로 재시도합니다.
    일시적 오류(네트워크, 429 rate limit 등)만 재시도하고,
    인증 오류(401), 잘못된 요청(400) 등 영구적 오류는 즉시 raise합니다.
    """
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
            )
            return response
        except Exception as e:
            if attempt == max_retries - 1 or not _is_retryable_error(e):
                raise
            delay = base_delay * (2 ** attempt)
            logging.warning(
                f"API call failed (attempt {attempt + 1}/{max_retries}): {e}. "
                f"Retrying in {delay}s..."
            )
            time.sleep(delay)


def init_config_only(log_filename=None, config_filename=DEFAULT_CONFIG_FILENAME):
    """
    API 호출이 필요 없는 스크립트용 초기화: 로깅 설정 + config 로드만 수행.
    Returns config or None on failure.
    """
    setup_logging(log_filename)
    config = load_config(config_filename)
    return config


def init_pipeline(log_filename=None, config_filename=DEFAULT_CONFIG_FILENAME):
    """
    공통 파이프라인 초기화: 로깅 설정, config 로드, Gemini 클라이언트 생성.
    Returns (config, client) or (None, None) on failure.
    """
    config = init_config_only(log_filename, config_filename)
    if not config:
        return None, None

    api_key = get_api_key(config)
    if not api_key:
        return None, None

    client = genai.Client(api_key=api_key)
    return config, client
