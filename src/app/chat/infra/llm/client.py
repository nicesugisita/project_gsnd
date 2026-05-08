"""LLM용 httpx 싱글턴 클라이언트 관리"""

from typing import Optional

import httpx

from app.core.config import Config

_llm_client: Optional[httpx.AsyncClient] = None
_LLM_POOL_LIMITS = httpx.Limits(max_keepalive_connections=5, keepalive_expiry=30)

# 커넥션 풀 stale 연결로 인한 RuntimeError / RemoteProtocolError 재시도 대상
_POOL_RETRY_ERRORS = (RuntimeError, httpx.RemoteProtocolError)


def _get_llm_client() -> httpx.AsyncClient:
    """LLM용 모듈 레벨 httpx 클라이언트 (재사용, lazy 초기화)"""
    global _llm_client
    if _llm_client is None or _llm_client.is_closed:
        _llm_client = httpx.AsyncClient(timeout=Config.LLM_API_TIMEOUT, limits=_LLM_POOL_LIMITS)
    return _llm_client


async def _reset_llm_client():
    """커넥션 풀 오류 시 싱글턴 폐기 — 열린 소켓을 닫고 재생성"""
    global _llm_client
    old = _llm_client
    _llm_client = None
    if old and not old.is_closed:
        await old.aclose()
