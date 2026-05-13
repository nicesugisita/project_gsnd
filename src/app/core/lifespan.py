"""
Application lifespan — startup/shutdown 자원 관리.

무거운 자원(httpx 클라이언트, DB 풀, JVM)은 startup 시 1회만 초기화하여
app.state에 저장하고, shutdown 시 명시적으로 해제한다.

접근 방법:
    # FastAPI 핸들러 / 의존성에서
    from fastapi import Request
    client = request.app.state.llm_client
    pool   = request.app.state.db_pool
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# httpx 커넥션 풀 설정 (모듈 상수)
_LLM_POOL_LIMITS = httpx.Limits(max_keepalive_connections=10, keepalive_expiry=30)
_DS_POOL_LIMITS  = httpx.Limits(max_keepalive_connections=5,  keepalive_expiry=30)


# ─────────────────────────────────────────────────────────────────────────────
# Startup helpers
# ─────────────────────────────────────────────────────────────────────────────

def _init_jvm() -> None:
    from app.mariner.jvm_manager import init_jvm
    try:
        init_jvm()
        logger.info("[Startup] JVM 초기화 완료")
    except Exception:
        logger.exception("[Startup] JVM 초기화 실패 — Mariner 검색 비활성화됨")


def _init_retriever():
    """MarinerRetriever 생성 (JVM 초기화 후 호출해야 함)."""
    try:
        from app.chat.infra.retriever import MarinerRetriever
        retriever = MarinerRetriever()
        logger.info("[Startup] MarinerRetriever 초기화 완료")
        return retriever
    except Exception:
        logger.exception("[Startup] MarinerRetriever 초기화 실패")
        return None


def _init_llm_client(settings) -> httpx.AsyncClient:
    """LLM HTTP 클라이언트 생성 후 모듈 전역변수에도 주입 (기존 호출 코드 호환)."""
    client = httpx.AsyncClient(
        timeout=settings.LLM_API_TIMEOUT,
        limits=_LLM_POOL_LIMITS,
    )
    # 기존 모듈 수준 전역변수(_llm_client)에도 주입해 하위 호환 유지
    from app.chat.infra.llm import client as _llm_mod
    _llm_mod._llm_client = client
    logger.info("[Startup] LLM httpx 클라이언트 초기화 완료")
    return client


def _init_ds_client(settings) -> httpx.AsyncClient:
    """DeepServer HTTP 클라이언트 생성 후 모듈 전역변수에도 주입."""
    client = httpx.AsyncClient(
        timeout=settings.DEEPSERVER_TIMEOUT,
        limits=_DS_POOL_LIMITS,
    )
    from app.chat.infra.deepserver import client as _ds_mod
    _ds_mod._ds_client = client
    logger.info("[Startup] DeepServer httpx 클라이언트 초기화 완료")
    return client


def _init_db_pool(settings):
    """MySQL 커넥션 풀 초기화. DB 미설정 시 None 반환."""
    if not settings.DB_HOST:
        logger.warning("[Startup] DB_HOST 미설정 — DB 커넥션 풀 건너뜀")
        return None
    try:
        import mysql.connector.pooling
        pool = mysql.connector.pooling.MySQLConnectionPool(
            pool_name=settings.DB_POOL_NAME,
            pool_size=settings.DB_POOL_SIZE,
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            database=settings.DB_NAME,
            autocommit=True,
            connection_timeout=settings.DB_CONNECTION_TIMEOUT,
        )
        logger.info(
            "[Startup] DB 커넥션 풀 초기화 완료 (pool_size=%d, host=%s:%d)",
            settings.DB_POOL_SIZE, settings.DB_HOST, settings.DB_PORT,
        )
        return pool
    except Exception:
        logger.exception("[Startup] DB 커넥션 풀 초기화 실패 — 요청마다 단건 연결 사용")
        return None


def _preload_prompts() -> None:
    """lru_cache 프롬프트 워밍업."""
    from app.shared.utils.prompt_loader import (
        load_system_prompt, load_query_reform_prompt, load_query_expansion_prompt,
        load_triple_extraction_prompt, load_convert_korean_prompt,
        load_ask_judgment_prompt, load_re_ask_prompt,
        load_rag_norag_judgment_prompt,
        load_text_cleaning_prompt, load_voice_cleaning_prompt,
        load_final_response_prompt, load_general_or_care_prompt,
        load_query_recreation_prompt, load_suggest_questions_prompt,
        load_voice_print_before_prompt, load_intent_classification_prompt,
        load_classification_general_prompt, load_classification_comparison_prompt,
        load_comparison_extract_prompt, load_comparison_attribute_prompt,
        load_comparison_triple_prompt, load_classification_recommended_prompt,
        load_classification_llm_recommended_prompt,
        load_classification_search_prompt,
        load_region_age_collect_recommended_prompt,
        load_document_summary_prompt, load_uploaded_qa_prompt,
    )
    loaders = [
        load_system_prompt, load_query_reform_prompt, load_query_expansion_prompt,
        load_triple_extraction_prompt, load_convert_korean_prompt,
        load_ask_judgment_prompt, load_re_ask_prompt,
        load_rag_norag_judgment_prompt,
        load_text_cleaning_prompt, load_voice_cleaning_prompt,
        load_final_response_prompt, load_general_or_care_prompt,
        load_query_recreation_prompt, load_suggest_questions_prompt,
        load_voice_print_before_prompt, load_intent_classification_prompt,
        load_classification_general_prompt, load_classification_comparison_prompt,
        load_comparison_extract_prompt, load_comparison_attribute_prompt,
        load_comparison_triple_prompt, load_classification_recommended_prompt,
        load_classification_llm_recommended_prompt,
        load_classification_search_prompt,
        load_region_age_collect_recommended_prompt,
        load_document_summary_prompt, load_uploaded_qa_prompt,
    ]
    for loader in loaders:
        loader()
    logger.info("[Startup] 프롬프트 %d개 프리로드 완료", len(loaders))


def _start_policy_priority_refresh_worker() -> None:
    """정책 우선순위 키워드 변경 체크 워커 시작."""
    try:
        from app.chat.infra.rag.policy_priority import (
            preload_policy_priority_cache,
            start_policy_priority_refresh_worker,
        )
        preload_policy_priority_cache()
        start_policy_priority_refresh_worker()
    except Exception:
        logger.exception("[Startup] Policy priority refresh worker 시작 실패")


def _stop_policy_priority_refresh_worker() -> None:
    """정책 우선순위 키워드 변경 체크 워커 종료."""
    try:
        from app.chat.infra.rag.policy_priority import stop_policy_priority_refresh_worker
        stop_policy_priority_refresh_worker()
    except Exception:
        logger.exception("[Shutdown] Policy priority refresh worker 종료 실패")


# ─────────────────────────────────────────────────────────────────────────────
# Shutdown helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _close_http_client(name: str, client: httpx.AsyncClient | None) -> None:
    if client is None:
        return
    try:
        if not client.is_closed:
            await client.aclose()
            logger.info("[Shutdown] %s httpx 클라이언트 닫힘", name)
    except Exception:
        logger.exception("[Shutdown] %s 클라이언트 종료 중 오류", name)


def _close_db_pool(pool) -> None:
    if pool is None:
        return
    try:
        # mysql-connector-python 풀은 명시적 close API가 없음
        # 내부 커넥션을 개별 해제
        import mysql.connector.pooling
        while True:
            try:
                conn = pool.get_connection()
                conn.close()
            except Exception:
                break
        logger.info("[Shutdown] DB 커넥션 풀 해제 완료")
    except Exception:
        logger.exception("[Shutdown] DB 풀 해제 중 오류")


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI asynccontextmanager lifespan.

    app.state 에 저장되는 자원:
        llm_client  : httpx.AsyncClient  — LLM API 커넥션 풀
        ds_client   : httpx.AsyncClient  — DeepServer API 커넥션 풀
        db_pool     : MySQLConnectionPool | None
    """
    settings = get_settings()
    logger.info("[Startup] 애플리케이션 기동 시작 (env=%s)", settings.ENVIRONMENT)

    # ── Startup ───────────────────────────────────────────────────────────────
    _init_jvm()

    app.state.llm_client = _init_llm_client(settings)
    app.state.ds_client  = _init_ds_client(settings)
    app.state.db_pool    = _init_db_pool(settings)
    app.state.retriever  = _init_retriever()   # JVM 초기화 이후에 생성

    _preload_prompts()
    _start_policy_priority_refresh_worker()

    logger.info("[Startup] 모든 자원 초기화 완료 — 서버 준비됨")

    yield  # ← 여기서 앱이 요청을 처리

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("[Shutdown] 애플리케이션 종료 — 자원 해제 시작")

    await _close_http_client("LLM",        app.state.llm_client)
    await _close_http_client("DeepServer", app.state.ds_client)
    _close_db_pool(app.state.db_pool)
    _stop_policy_priority_refresh_worker()

    logger.info("[Shutdown] 모든 자원 해제 완료")
