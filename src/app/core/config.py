"""
Application configuration — pydantic-settings 기반 환경변수 중앙화.

사용법:
    from app.core.config import get_settings, Config

    settings = get_settings()          # @lru_cache 싱글톤
    Config.LLM_API_URL                 # 하위 호환: Config = get_settings()
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple, Type

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, SettingsConfigDict

# .env 파일 위치: src/app/core/ 기준 3단계 위 (프로젝트 루트)
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

# List/Dict 필드 중 JSON이 아닌 커스텀 포맷(.env에 콤마 구분값)을 가진 필드 목록
_NON_JSON_COMPLEX_FIELDS = {"ALLOWED_BASE_DIRS", "BASE_PATH_ALIASES", "CORS_ORIGINS",
                             "ASSISTANT_ACTION_FEEDBACK_OPTIONS"}


class _SafeDotEnvSource(DotEnvSettingsSource):
    """
    pydantic-settings 2.x의 기본 DotEnv 소스 대체제.

    List/Dict 필드를 JSON으로 파싱하기 전, 커스텀 포맷(콤마 구분 등)의 값을
    그대로 문자열로 전달하여 field_validator가 처리하도록 한다.
    """

    def __init__(self, settings_cls, env_file=None, **kwargs):
        super().__init__(settings_cls, env_file=env_file or _ENV_FILE)

    def prepare_field_value(
        self,
        field_name: str,
        field_info,
        value: Any,
        value_is_complex: bool,
    ) -> Any:
        # 커스텀 포맷 필드는 raw 문자열 그대로 반환 → field_validator가 파싱
        if field_name.upper() in _NON_JSON_COMPLEX_FIELDS and isinstance(value, str):
            return value
        return super().prepare_field_value(field_name, field_info, value, value_is_complex)


class Settings(BaseSettings):
    """
    모든 설정값의 단일 진실 공급원(Single Source of Truth).

    우선순위: 환경변수 > .env 파일 > 필드 기본값
    시크릿(비밀번호, API 키 등)은 코드에 기본값을 두지 않고 .env에서만 로드.
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,    # DB_HOST == db_host
        extra="ignore",          # .env에 알 수 없는 키가 있어도 무시
        env_ignore_empty=True,   # 빈 env 값("") → 필드 기본값 사용 (JSON 파싱 오류 방지)
    )

    # ── 서버 ──────────────────────────────────────────────────────────────────
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "development"  # development | production | testing

    # ── 애플리케이션 정보 ─────────────────────────────────────────────────────
    APP_NAME: str = "경상남도청 RAG 챗봇 API"
    APP_VERSION: str = "1.0.0"
    APP_DESCRIPTION: str = "Retrieval Augmented Generation을 활용한 AI 챗봇"

    # ── 모델 ──────────────────────────────────────────────────────────────────
    MODEL_NAME: str = ""

    # ── LLM ───────────────────────────────────────────────────────────────────
    LLM_ENABLED: bool = True
    # 스트리밍 MORE_INFO 후속 의도 분류 LLM. False면 항상 OTHER(폴백), 호출 생략.
    NEXT_INTENT_LLM_ENABLED: bool = True
    LLM_API_URL: str = ""
    LLM_API_TIMEOUT: int = 120
    RELEVANCE_LLM_API_URL: str = ""
    RELEVANCE_LLM_MODEL_NAME: str = ""
    FIXED_LLM_SEED: int = 42

    # ── STT ───────────────────────────────────────────────────────────────────
    STT_WEBSOCKET_URL: str = ""
    STT_PING_INTERVAL: int = 20
    STT_PING_TIMEOUT: int = 10
    STT_CLOSE_TIMEOUT: int = 5
    STT_CONNECTION_TIMEOUT: float = 10.0

    # ── TTS ───────────────────────────────────────────────────────────────────
    TTS_SERVER_URL: str = ""
    TTS_MASTER_KEY: str = ""          # 시크릿: .env 필수
    TTS_REQUEST_TIMEOUT: float = 30.0
    SID: int = 0
    TEMPO: int = 1
    PAD_SILENCE: int = 0
    AMPLIFY: int = 1
    GAIN_DB: int = 0

    # ── 전처리 토글 ───────────────────────────────────────────────────────────
    TEXT_CLEANING_ENABLED: bool = True
    KOREAN_STANDARDIZATION_ENABLED: bool = True

    # SLM 기반 문서 관련성 필터(filter_irrelevant_docs) 사용 여부.
    # False 시 필터를 스킵하고 각 파이프라인의 FINAL_TOP_N 을 상향(+50%대)하여
    # 후처리 dedupe 만으로 노이즈 문서를 흡수할 수 있는지 측정한다.
    # A/B 측정 절차: .env 에서 토글만 바꿔 동일 질의 세트를 두 번 실행 후
    # referenced_documents / 최종 응답을 비교 (RELEVANCE_FILTER_ENABLED=true/false).
    RELEVANCE_FILTER_ENABLED: bool = True

    # ── DeepServer ────────────────────────────────────────────────────────────
    DEEP_SERVER_URL: str = ""
    DEEPSERVER_TIMEOUT: float = 30.0
    MULTITURN_MAX_TURNS: int = 3
    MULTITURN_ASSISTANT_SUMMARY_LEN: int = 150

    # ── RAG ───────────────────────────────────────────────────────────────────
    RAG_ENABLED: bool = True
    RAG_COLLECTION: str = "GSND_DATASET_V8"                  # service_target='official'
    RAG_CITIZEN_COLLECTION: str = "GSND_DATASET_V8_CITIZEN"  # service_target='citizen'
    RAG_THRESHOLD: float = 0.0
    RAG_USE_QA_WHEN_EMPTY: bool = True
    RAG_NUM_REFERENCED_DOCS: int = 5
    RAG_UI_MAX_DOCS: int = 5
    RAG_OKMS_COLLECTION: str = ""
    RAG_GOV_OKMS_COLLECTION: str = ""
    RAG_WELFARE_CENTER_COLLECTION: str = ""
    RAG_WELFARE_TEL_COLLECTION: str = ""

    # Query Rewriting 모드 토글.
    # False(기본): unified_preprocessing_prompt.txt 사용 — Task 4 의미 보존형 expansion 5개 생성.
    # True       : unified_preprocessing_prompt_rewrite.txt 사용 — 단일 self-contained 쿼리 1개로 검색.
    # rewrite 모드는 대화 맥락 흡수·모호함 해소·키워드 강화로 정밀도 ↑, Mariner 호출 수 ↓.
    # 운영에서 .env 토글로 A/B 비교 후 default 전환 검토.
    QUERY_REWRITING_ENABLED: bool = True

    # 분류기 단축 프롬프트 우선 로드 토글. True 면 prompts/short/<filename> 가 존재할 때
    # 그것을 우선 사용한다. unified_preprocessing/pre_check/next_intent 등 분류기 프롬프트의
    # 압축본(원본 대비 60~83% 단축)을 운영에 적용할 때 켠다. 기본 False 로 회귀 위험 차단.
    USE_SHORT_PROMPTS: bool = False

    # 응답 트레이스 토글 — 매 응답 생성마다 (사용자 질문 / 참조문서 / 최종 프롬프트 / 응답 본문)
    # 을 JSONL + xlsx 두 파일에 기록한다. 디버그·QA 목적. 운영에선 비활성 권장.
    RESPONSE_TRACE_ENABLED: bool = False
    # 트레이스 출력 디렉토리. 없으면 자동 생성. JSONL: response_trace.jsonl, xlsx: response_trace.xlsx
    RESPONSE_TRACE_DIR: str = "log/response_trace"

    # ── Mariner 연결 ──────────────────────────────────────────────────────────
    MARINER_IP: str = ""
    MARINER_PORT: int = 5555
    MARINER_TIMEOUT: int = 60000
    MARINER_THRESHOLD: float = 0.0
    MARINER_MAX_RESULTS: int = 5
    MARINER_LOCAL_HOST: str = "localhost"
    MARINER_LOCAL_PORT: str = "5555"

    # ── Mariner Index API ─────────────────────────────────────────────────────
    MARINER_INDEX_API_URL: str = ""
    MARINER_UPLOAD_COLLECTION: str = ""
    MARINER_INDEX_TIMEOUT: int = 30

    # ── 파일 업로드 ───────────────────────────────────────────────────────────
    UPLOAD_DIR: str = ""

    # ── 원격 텍스트 전송 ──────────────────────────────────────────────────────
    UPLOADED_TEXT_REMOTE_ENABLED: bool = True
    UPLOADED_TEXT_REMOTE_HOST: str = ""
    UPLOADED_TEXT_REMOTE_PORT: int = 22
    UPLOADED_TEXT_REMOTE_USER: str = ""
    UPLOADED_TEXT_REMOTE_DIR: str = ""
    UPLOADED_TEXT_REMOTE_TIMEOUT: int = 30
    UPLOADED_TEXT_REMOTE_SSH_KEY_PATH: str = ""
    UPLOADED_TEXT_REMOTE_PASSWORD: str = ""   # 시크릿: .env 필수

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ORIGINS: List[str] = ["*"]

    # ── JVM / JAR ─────────────────────────────────────────────────────────────
    JAR_LIB_PATH: str = ""
    JF_ATTACHER_CONF_PATH: str = ""
    JF_ATTACHER_NATIVE_LIB_PATH: str = ""
    JAVA_MIN_MEMORY: str = "32m"
    JAVA_MAX_MEMORY: str = "512m"

    # ── DB (MariaDB / MySQL) ──────────────────────────────────────────────────
    DB_HOST: str = ""
    DB_PORT: int = 3306
    DB_USER: str = ""
    DB_PASSWORD: str = ""             # 시크릿: .env 필수
    DB_NAME: str = ""
    OKMS2_DB_NAME: str = ""
    DB_CONNECTION_TIMEOUT: int = 5
    DB_POOL_SIZE: int = 5             # 커넥션 풀 크기 (lifespan에서 사용)
    DB_POOL_NAME: str = "gsnd_pool"
    POLICY_PRIORITY_DB_ENABLED: bool = True
    POLICY_PRIORITY_TABLE: str = "gsnd_policy_priority"
    POLICY_PRIORITY_CACHE_TTL_SEC: int = 1800
    POLICY_PRIORITY_CHANGE_CHECK_SEC: int = 1800
    # 정책 정적 규칙 YAML(우선순위 tag 별 anchor_keywords 등). 프로젝트 루트 기준 상대경로 허용.
    POLICY_RULES_PATH: str = "config/policy_rules.yaml"

    # ── 세션 ──────────────────────────────────────────────────────────────────
    SESSION_TIMEOUT_MINUTES: int = 10
    SESSION_TABLE: str = ""
    MAX_CONCURRENT_USERS: int = 50
    CONV_LOCKS_MAX: int = 1000
    # 비로그인 앱 전용 세션 문자열 등 → 히스토리 키는 nologin{conv_id} 로 통일. 콤마 구분 접두어(소문 비교).
    # 비어 두면 코드 기본 목록 사용.
    CHAT_EPHEMERAL_USER_ID_PREFIXES: str = ""
    # True면 UUID v4 형태 user_id 를 비로그인으로 간주한다(실제 로그인 ID가 UUID 면 비활성 유지).
    CHAT_USER_ID_IS_EPHEMERAL_IF_UUIDV4: bool = False
    DEFAULT_CONVERSATION_TITLE: str = "새로운 대화"

    # ── 되묻기 ────────────────────────────────────────────────────────────────
    MAX_CLARIFY_ATTEMPTS: int = 3
    CLARIFY_FAILURE_MESSAGE: str = ""

    # ── 추천 질문 ─────────────────────────────────────────────────────────────
    MAX_SUGGESTED_QUESTIONS: int = 5

    # ── 문서 서비스 ───────────────────────────────────────────────────────────
    OKMS_DOC_TABLE: str = ""
    OKMS_VIEW_TABLE: str = ""
    ALLOWED_BASE_DIRS: List[str] = []
    BASE_PATH_ALIASES: Dict[str, str] = {}

    # ── 인사말 ────────────────────────────────────────────────────────────────
    GREETING_MESSAGE: str = ""

    # ── 피드백 (상수성 값, env 오버라이드 불필요) ──────────────────────────────
    ASSISTANT_ACTION_FEEDBACK: str = "feedback"
    ASSISTANT_ACTION_FEEDBACK_OPTIONS: List[str] = ["like", "dislike"]

    # ── 검증자 ────────────────────────────────────────────────────────────────

    @field_validator("ALLOWED_BASE_DIRS", mode="before")
    @classmethod
    def _parse_allowed_base_dirs(cls, v: Any) -> Any:
        """콤마 구분 문자열 또는 JSON 배열 → List[str]."""
        if isinstance(v, list):
            return v
        if isinstance(v, str) and v:
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
            return [x.strip() for x in v.split(",") if x.strip()]
        return []

    @field_validator("BASE_PATH_ALIASES", mode="before")
    @classmethod
    def _parse_base_path_aliases(cls, v: Any) -> Any:
        """'key:val,key:val' 또는 JSON dict → Dict[str, str]."""
        if isinstance(v, dict):
            return v
        if isinstance(v, str) and v:
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
            result: Dict[str, str] = {}
            for pair in v.split(","):
                if ":" in pair:
                    k, val = pair.split(":", 1)
                    result[k.strip()] = val.strip()
            return result
        return {}

    # ── 커스텀 env 소스: List/Dict 필드의 비-JSON 값 처리 ─────────────────────

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ) -> Tuple:
        """List/Dict 필드의 콤마 구분 값을 JSON 파싱 전에 보호하는 소스 체인."""
        return (
            init_settings,
            env_settings,
            _SafeDotEnvSource(settings_cls, env_file=dotenv_settings.env_file),
            file_secret_settings,
        )

    @model_validator(mode="after")
    def _apply_environment_defaults(self) -> "Settings":
        """
        ENVIRONMENT 값에 따라 명시적으로 설정되지 않은 필드에 환경별 기본값 적용.
        .env에서 직접 설정한 값은 덮어쓰지 않는다.
        """
        env = self.ENVIRONMENT.lower()
        explicitly_set = self.model_fields_set

        if env == "development":
            if "debug" not in explicitly_set and "DEBUG" not in explicitly_set:
                object.__setattr__(self, "DEBUG", True)
            if "log_level" not in explicitly_set and "LOG_LEVEL" not in explicitly_set:
                object.__setattr__(self, "LOG_LEVEL", "DEBUG")

        elif env == "testing":
            if "rag_enabled" not in explicitly_set and "RAG_ENABLED" not in explicitly_set:
                object.__setattr__(self, "RAG_ENABLED", False)
            if "llm_enabled" not in explicitly_set and "LLM_ENABLED" not in explicitly_set:
                object.__setattr__(self, "LLM_ENABLED", False)
            if "log_level" not in explicitly_set and "LOG_LEVEL" not in explicitly_set:
                object.__setattr__(self, "LOG_LEVEL", "DEBUG")

        return self


@lru_cache
def get_settings() -> Settings:
    """Settings @lru_cache 싱글톤 — 프로세스당 1회만 생성."""
    return Settings()


def get_config() -> Settings:
    """get_settings() 하위 호환 alias."""
    return get_settings()


# ── 하위 호환: 기존 코드의 `Config.XXX` 클래스 속성 접근 패턴 유지 ───────────
# Config는 Settings 인스턴스(싱글톤)이므로 Config.XXX == get_settings().XXX
Config: Settings = get_settings()
