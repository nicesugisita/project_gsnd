
"""
Application configuration module.

Supports environment-based configuration for development, production, and testing environments.
"""

import json
import os
from typing import Dict, List
from dotenv import load_dotenv


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, "../.env"))


def _parse_bool(value: str, default: bool = False) -> bool:
    """Parse string to boolean value."""
    return value.lower() in ('true', '1', 'yes') if value else default


def _parse_list(value: str, default: List[str] = None) -> List[str]:
    """Parse comma-separated string to list."""
    if not value:
        return default or []
    return [v.strip() for v in value.split(',') if v.strip()]


def _parse_dict(value: str, default: Dict[str, str] = None) -> Dict[str, str]:
    """Parse 'key:val,key:val' string to dict (colon-separated pairs, comma-delimited)."""
    if not value:
        return default or {}
    result = {}
    for pair in value.split(','):
        if ':' in pair:
            k, v = pair.split(':', 1)
            result[k.strip()] = v.strip()
    return result


class Config:
    """Base configuration class."""

    # ========================================================================
    # Server Configuration
    # ========================================================================
    HOST: str = os.getenv('HOST', '0.0.0.0')
    PORT: int = int(os.getenv('PORT', 8000))
    DEBUG: bool = _parse_bool(os.getenv('DEBUG', 'False'))
    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO')

    # ========================================================================
    # Application Information
    # ========================================================================
    APP_NAME: str = "경상남도청 RAG 챗봇 API"
    APP_VERSION: str = "1.0.0"
    APP_DESCRIPTION: str = "Retrieval Augmented Generation을 활용한 AI 챗봇"

    # ========================================================================
    # Model Configuration
    # ========================================================================
    MODEL_NAME: str = os.getenv('MODEL_NAME', '')

    # ========================================================================
    # LLM Configuration
    # ========================================================================
    LLM_ENABLED: bool = _parse_bool(os.getenv('LLM_ENABLED', 'True'))
    LLM_API_URL: str = os.getenv('LLM_API_URL', '')
    LLM_API_TIMEOUT: int = int(os.getenv('LLM_API_TIMEOUT', 120))
    RELEVANCE_LLM_API_URL: str = os.getenv('RELEVANCE_LLM_API_URL', '')
    RELEVANCE_LLM_MODEL_NAME: str = os.getenv('RELEVANCE_LLM_MODEL_NAME', '')

    # ========================================================================
    # STT Configuration
    # ========================================================================
    STT_WEBSOCKET_URL: str = os.getenv('STT_WEBSOCKET_URL', '')
    STT_PING_INTERVAL: int = int(os.getenv('STT_PING_INTERVAL', 20))
    STT_PING_TIMEOUT: int = int(os.getenv('STT_PING_TIMEOUT', 10))
    STT_CLOSE_TIMEOUT: int = int(os.getenv('STT_CLOSE_TIMEOUT', 5))
    STT_CONNECTION_TIMEOUT: float = float(os.getenv('STT_CONNECTION_TIMEOUT', 10.0))

    # ========================================================================
    # TTS Configuration
    # ========================================================================
    TTS_SERVER_URL: str = os.getenv('TTS_SERVER_URL', '')
    TTS_MASTER_KEY: str = os.getenv('TTS_MASTER_KEY', '')
    TTS_REQUEST_TIMEOUT: float = float(os.getenv('TTS_REQUEST_TIMEOUT', 30.0))
    ## TTS 서비스 매니저 텍스트 음성변환 api (/service/ttsstrem) 호출 시 필요한 payload
    SID : int = os.getenv('SID', 0)
    TEMPO : int = os.getenv('TEMPO', 1)
    PAD_SILENCE : int = os.getenv('PAD_SILENCE', 0)
    AMPLIFY : int = os.getenv('AMPLIFY', 1)
    GAIN_DB : int = os.getenv('GAIN_DB', 0)


    # ========================================================================
    # Preprocessing Feature Toggles
    # ========================================================================
    TEXT_CLEANING_ENABLED: bool = _parse_bool(os.getenv('TEXT_CLEANING_ENABLED', 'True'))
    KOREAN_STANDARDIZATION_ENABLED: bool = _parse_bool(os.getenv('KOREAN_STANDARDIZATION_ENABLED', 'True'))

    # ========================================================================
    # DeepServer Configuration
    # ========================================================================
    DEEP_SERVER_URL: str = os.getenv('DEEP_SERVER_URL', '')
    DEEPSERVER_TIMEOUT: float = float(os.getenv('DEEPSERVER_TIMEOUT', 30.0))
    # 멀티턴 RAG/NO-RAG 판단 시 포함할 최대 대화 턴 수 (1턴 = user+assistant 1쌍, 메시지 2개)
    MULTITURN_MAX_TURNS: int = int(os.getenv('MULTITURN_MAX_TURNS', 3))
    # 멀티턴 RAG/NO-RAG 판단 시 assistant 응답 요약 길이 (0 = 전체 사용)
    MULTITURN_ASSISTANT_SUMMARY_LEN: int = int(os.getenv('MULTITURN_ASSISTANT_SUMMARY_LEN', 150))

    # ========================================================================
    # RAG Configuration
    # ========================================================================
    RAG_ENABLED: bool = _parse_bool(os.getenv('RAG_ENABLED', 'True'))
    RAG_COLLECTION: str = os.getenv('RAG_COLLECTION', '')
    RAG_THRESHOLD: float = float(os.getenv('RAG_THRESHOLD', 0))
    RAG_USE_QA_WHEN_EMPTY: bool = _parse_bool(
        os.getenv('RAG_USE_QA_WHEN_EMPTY', 'True')
    )
    # 참조 문서 개수 설정
    RAG_NUM_REFERENCED_DOCS: int = int(os.getenv('RAG_NUM_REFERENCED_DOCS', 5))
    # UI에 표시할 참조 문서 최대 개수 (LLM 컨텍스트 문서 수와 독립적으로 관리)
    RAG_UI_MAX_DOCS: int = int(os.getenv('RAG_UI_MAX_DOCS', 5))
    # guide_recommend / comparison 분기 전용 OKMS 컬렉션
    RAG_OKMS_COLLECTION: str = os.getenv('RAG_OKMS_COLLECTION', '')
    RAG_GOV_OKMS_COLLECTION: str = os.getenv('RAG_GOV_OKMS_COLLECTION', '')
    # 복지시설 검색 전용 컬렉션
    RAG_WELFARE_CENTER_COLLECTION: str = os.getenv('RAG_WELFARE_CENTER_COLLECTION', '')
    # 복지 문의처 검색 전용 컬렉션
    RAG_WELFARE_TEL_COLLECTION: str = os.getenv('RAG_WELFARE_TEL_COLLECTION', '')
    # 적합성 판단(RetrievalJudgment) 활성화 여부 — False 시 항상 sufficient=false로 간주
    RETRIEVAL_JUDGMENT_ENABLED: bool = _parse_bool(os.getenv('RETRIEVAL_JUDGMENT_ENABLED', 'False'))

    # ========================================================================
    # Mariner Connection Configuration
    # ========================================================================
    MARINER_IP: str = os.getenv('MARINER_IP', '')
    MARINER_PORT: int = int(os.getenv('MARINER_PORT', 5555))
    MARINER_TIMEOUT: int = int(os.getenv('MARINER_TIMEOUT', 60000))
    MARINER_THRESHOLD: float = float(os.getenv('MARINER_THRESHOLD', 0.5))
    MARINER_MAX_RESULTS: int = int(os.getenv('MARINER_MAX_RESULTS', 5))
    MARINER_LOCAL_HOST: str = os.getenv('MARINER_LOCAL_HOST', 'localhost')
    MARINER_LOCAL_PORT: str = os.getenv('MARINER_LOCAL_PORT', '5555')

    # ========================================================================
    # Mariner Index API Configuration
    # ========================================================================
    MARINER_INDEX_API_URL: str = os.getenv('MARINER_INDEX_API_URL', '')
    MARINER_UPLOAD_COLLECTION: str = os.getenv('MARINER_UPLOAD_COLLECTION', '')
    MARINER_INDEX_TIMEOUT: int = int(os.getenv('MARINER_INDEX_TIMEOUT', 30))

    # ========================================================================
    # File Upload Configuration
    # ========================================================================
    UPLOAD_DIR: str = os.getenv('UPLOAD_DIR', '')

    # ========================================================================
    # Uploaded Text Remote Transfer
    # ========================================================================
    UPLOADED_TEXT_REMOTE_ENABLED: bool = _parse_bool(
        os.getenv('UPLOADED_TEXT_REMOTE_ENABLED', 'True')
    )
    UPLOADED_TEXT_REMOTE_HOST: str = os.getenv('UPLOADED_TEXT_REMOTE_HOST', '')
    UPLOADED_TEXT_REMOTE_PORT: int = int(os.getenv('UPLOADED_TEXT_REMOTE_PORT', 22))
    UPLOADED_TEXT_REMOTE_USER: str = os.getenv('UPLOADED_TEXT_REMOTE_USER', '')
    UPLOADED_TEXT_REMOTE_DIR: str = os.getenv('UPLOADED_TEXT_REMOTE_DIR', '')
    UPLOADED_TEXT_REMOTE_TIMEOUT: int = int(os.getenv('UPLOADED_TEXT_REMOTE_TIMEOUT', 30))
    UPLOADED_TEXT_REMOTE_SSH_KEY_PATH: str = os.getenv('UPLOADED_TEXT_REMOTE_SSH_KEY_PATH', '')
    UPLOADED_TEXT_REMOTE_PASSWORD: str = os.getenv('UPLOADED_TEXT_REMOTE_PASSWORD', '')

    # ========================================================================
    # CORS Configuration
    # ========================================================================
    CORS_ORIGINS: List[str] = ["*"]
    # ========================================================================
    # JAR Library Path
    # ========================================================================
    JAR_LIB_PATH: str = os.getenv('JAR_LIB_PATH', '')

    # ========================================================================
    # Database Configuration (MariaDB)
    # ========================================================================
    DB_HOST: str = os.getenv('DB_HOST', '')
    DB_PORT: int = int(os.getenv('DB_PORT', 3306))
    DB_USER: str = os.getenv('DB_USER', '')
    DB_PASSWORD: str = os.getenv('DB_PASSWORD', '')
    DB_NAME: str = os.getenv('DB_NAME', '')
    OKMS2_DB_NAME: str = os.getenv('OKMS2_DB_NAME', '')
    DB_CONNECTION_TIMEOUT: int = int(os.getenv('DB_CONNECTION_TIMEOUT', 5))

    # ========================================================================
    # Session Configuration
    # ========================================================================
    SESSION_TIMEOUT_MINUTES: int = int(os.getenv('SESSION_TIMEOUT_MINUTES', 10)) # session expires after 10 minutes of inactivity
    SESSION_TABLE: str = os.getenv('SESSION_TABLE', '')
    MAX_CONCURRENT_USERS: int = int(os.getenv('MAX_CONCURRENT_USERS', 50))
    CONV_LOCKS_MAX: int = int(os.getenv('CONV_LOCKS_MAX', 1000))
    DEFAULT_CONVERSATION_TITLE: str = os.getenv('DEFAULT_CONVERSATION_TITLE', '새로운 대화')
    JAVA_MIN_MEMORY: str = os.getenv('JAVA_MIN_MEMORY', '32m')
    JAVA_MAX_MEMORY: str = os.getenv('JAVA_MAX_MEMORY', '512m')

    # ========================================================================
    # Clarification (Re-ask) Configuration
    # ========================================================================
    MAX_CLARIFY_ATTEMPTS: int = int(os.getenv('MAX_CLARIFY_ATTEMPTS', 3))
    CLARIFY_FAILURE_MESSAGE: str = os.getenv('CLARIFY_FAILURE_MESSAGE', '')

    # ========================================================================
    # Suggested Questions Configuration
    # ========================================================================
    MAX_SUGGESTED_QUESTIONS: int = int(os.getenv('MAX_SUGGESTED_QUESTIONS', 5))

    # ============================================================================
    # Assistant Action Types & Options
    # ============================================================================
    ASSISTANT_ACTION_FEEDBACK = "feedback"
    ASSISTANT_ACTION_FEEDBACK_OPTIONS = ["like", "dislike"]

    # ========================================================================
    # Document Service Configuration
    # ========================================================================
    OKMS_DOC_TABLE: str = os.getenv('OKMS_DOC_TABLE', '')
    OKMS_VIEW_TABLE: str = os.getenv('OKMS_VIEW_TABLE', '')
    ALLOWED_BASE_DIRS: List[str] = _parse_list(os.getenv('ALLOWED_BASE_DIRS', ''))
    BASE_PATH_ALIASES: Dict[str, str] = _parse_dict(os.getenv('BASE_PATH_ALIASES', ''))

    # ========================================================================
    # LLM Seed Configuration
    # ========================================================================
    FIXED_LLM_SEED: int = int(os.getenv("FIXED_LLM_SEED", "42"))

    # ========================================================================
    # Greeting Message
    # ========================================================================
    GREETING_MESSAGE: str = os.getenv('GREETING_MESSAGE', '')


class DevelopmentConfig(Config):
    """Development environment configuration."""

    DEBUG: bool = True
    LOG_LEVEL: str = "DEBUG"


class ProductionConfig(Config):
    """Production environment configuration."""

    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"


class TestingConfig(Config):
    """Testing environment configuration."""

    DEBUG: bool = False
    LOG_LEVEL: str = "DEBUG"
    RAG_ENABLED: bool = False
    LLM_ENABLED: bool = False


def get_config() -> Config:
    """Get configuration based on environment variable."""
    env = os.getenv('ENVIRONMENT', 'development').lower()

    if env == 'development':
        return DevelopmentConfig()
    elif env == 'testing':
        return TestingConfig()
    else:
        return ProductionConfig()
