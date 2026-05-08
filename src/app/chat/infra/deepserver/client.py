"""
DeepServer Service - DeepServer 호출 공통 모듈
"""

import json
import logging
import httpx
from typing import List, Dict, Any, Optional
from app.core.config import Config

from app.core.constants import ROLE_USER, ROLE_ASSISTANT
logger = logging.getLogger(__name__)

# ============================================================
# httpx 싱글턴 클라이언트 (DeepServer 전용, 커넥션 풀 재사용)
# ============================================================

_ds_client: Optional[httpx.AsyncClient] = None
_DS_POOL_LIMITS = httpx.Limits(max_keepalive_connections=5, keepalive_expiry=30)
_POOL_RETRY_ERRORS = (RuntimeError, httpx.RemoteProtocolError)


def _get_ds_client() -> httpx.AsyncClient:
    """DeepServer용 모듈 레벨 httpx 클라이언트 (재사용, lazy 초기화)"""
    global _ds_client
    if _ds_client is None or _ds_client.is_closed:
        _ds_client = httpx.AsyncClient(timeout=Config.DEEPSERVER_TIMEOUT, limits=_DS_POOL_LIMITS)
    return _ds_client


async def _reset_ds_client():
    """커넥션 풀 오류 시 싱글턴 폐기 — 열린 소켓을 닫고 재생성"""
    global _ds_client
    old = _ds_client
    _ds_client = None
    if old and not old.is_closed:
        await old.aclose()

_DEEP_SERVER_REQUERY_URL = f"{Config.DEEP_SERVER_URL}/rag/re-query"
_DEEP_SERVER_GENERATION_URL = f"{Config.DEEP_SERVER_URL}/rag/generation"

_EXPAND_PROMPT_ID = 2         # 쿼리확장
_TRIPLE_PROMPT_ID = 18        # 트리플추출_general
_COMP_ATTR_PROMPT_ID = 19     # 비교속성추출_general
_COMP_TRIPLE_PROMPT_ID = 20   # 비교트리플추출_general

_TEXT_CLEANING_PROMPT_ID = 22      # 텍스트 질의정제
_VOICE_CLEANING_PROMPT_ID = 23     # 음성 질의정제
_GENERAL_OR_CARE_PROMPT_ID = 24    # 일반/복지 의도분류
_ASK_JUDGMENT_PROMPT_ID = 25       # 질문 성립여부 판단
_INTENT_CLASSIFICATION_PROMPT_ID = 26  # 일반/비교/추천 의도분류
_RAG_NORAG_JUDGMENT_PROMPT_ID = 27     # RAG/NO-RAG 응답방식 판단



async def _ds_post(url: str, payload: dict) -> httpx.Response:
    """DeepServer POST 공통 래퍼 — stale 커넥션 오류 시 1회 재시도"""
    try:
        client = _get_ds_client()
        return await client.post(url, json=payload)
    except _POOL_RETRY_ERRORS as e:
        logger.warning("DeepServer 커넥션 풀 오류 → 클라이언트 재생성 후 재시도: %s", e)
        await _reset_ds_client()
        client = _get_ds_client()
        return await client.post(url, json=payload)


async def _call_sllm(question: str, prompt_id: int) -> str:
    """/rag/re-query 엔드포인트를 통한 순수 SLLM 호출"""
    return await _call_requery(question, prompt_id)


async def _call_requery(question: str, prompt_id: int) -> str:
    """/rag/re-query 엔드포인트를 통한 호출"""
    payload = {
        "QUESTION": question,
        "QA_MODEL": "SLLM",
        "USER_CONV_ID": "",
        "PROMPT_ID": prompt_id,
    }
    response = await _ds_post(_DEEP_SERVER_REQUERY_URL, payload)
    response.raise_for_status()
    data = response.json()
    if not data.get("RESULT"):
        raise RuntimeError(f"DeepServer re-query 오류 (PROMPT_ID={prompt_id}): {data.get('MESSAGE')}")
    return data.get("OUTPUT", "")


async def deepserver_clean_text_query(question: str) -> str:
    """텍스트 질의 정제 (PROMPT_ID: 22) - /rag/re-query 사용"""
    return await _call_requery(question, _TEXT_CLEANING_PROMPT_ID)


async def deepserver_clean_voice_query(question: str) -> str:
    """음성 질의 정제 (PROMPT_ID: 23) - /rag/re-query 사용"""
    return await _call_requery(question, _VOICE_CLEANING_PROMPT_ID)


async def deepserver_classify_general_or_care(question: str) -> str:
    """일반/복지 의도 분류 (PROMPT_ID: 24) → 'GENERAL' or 'WELFARE'"""
    return await _call_sllm(question, _GENERAL_OR_CARE_PROMPT_ID)


async def _call_generation(question: str, prompt_id: int, task: str = "classification") -> str:
    """/rag/generation 엔드포인트를 통한 SLLM 호출"""
    payload = {
        "QUESTION": question,
        "USER_CONV_ID": "",
        "KNOWLEDGE": "",
        "TASK": task,
        "QA_MODEL": "SLLM",
        "IS_STREAM": False,
        "PROMPT_ID": str(prompt_id),
    }
    response = await _ds_post(_DEEP_SERVER_GENERATION_URL, payload)
    response.raise_for_status()
    data = response.json()
    if not data.get("RESULT"):
        raise RuntimeError(f"DeepServer generation 오류 (PROMPT_ID={prompt_id}): {data.get('MESSAGE')}")
    output = data.get("OUTPUT", "")
    # 마크다운 코드블록 제거 (```json ... ``` 또는 ``` ... ```)
    if output.startswith("```"):
        output = output.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return output


async def deepserver_ask_judgment(question: str) -> str:
    """질문 성립여부 판단 (PROMPT_ID: 25) → JSON string"""
    return await _call_generation(question, _ASK_JUDGMENT_PROMPT_ID)


async def deepserver_intent_classification(question: str) -> str:
    """일반/비교/추천 의도 분류 (PROMPT_ID: 26) → JSON string"""
    return await _call_generation(question, _INTENT_CLASSIFICATION_PROMPT_ID)




def _build_multiturn_question(question: str, messages: list) -> str:
    """대화 이력을 자연어 형태로 구성해 현재 질문 앞에 붙인 문자열 반환.

    대명사적 후속 질문("그거 뭐야?", "2025년에는?")에서 SLLM이
    대화 맥락을 인식할 수 있도록, 이전 대화 주제를 자연어로 요약합니다.

    Args:
        question: 현재 사용자 질문
        messages: 전체 대화 메시지 목록 (현재 질문 포함)

    Returns:
        "[이전 대화]\\n사용자: ...\\n챗봇: ...\\n\\n[현재 질문]\\n..." 형태
    """
    if not messages:
        return question

    # 현재 질문(마지막 user 메시지)을 제외한 이전 대화 이력만 추출
    history = [m for m in messages if m.get("role") in (ROLE_USER, ROLE_ASSISTANT)]
    if history and history[-1].get("role") == "user" and history[-1].get("content") == question:
        history = history[:-1]

    # 최근 N턴
    max_messages = Config.MULTITURN_MAX_TURNS * 2
    history = history[-max_messages:] if len(history) > max_messages else history

    if not history:
        return question

    # 자연어 포맷 구성 (assistant 응답은 요약)
    lines = []
    for m in history:
        role = m.get("role", "")
        content = str(m.get("content", "")).strip()
        if role == "user":
            lines.append(f"사용자: {content}")
        elif role == ROLE_ASSISTANT:
            summary_len = Config.MULTITURN_ASSISTANT_SUMMARY_LEN
            if summary_len > 0 and len(content) > summary_len:
                content = content[:summary_len] + "..."
            lines.append(f"챗봇: {content}")

    history_text = "\n".join(lines)
    return (
        f"[이전 대화]\n{history_text}\n\n"
        f"[현재 질문]\n{question}\n\n"
        f"위 대화 맥락을 고려하여 현재 질문의 RAG 필요 여부를 판단하십시오."
    )


async def deepserver_rag_norag_judgment(question: str, messages: list = None) -> str:
    """RAG/NO-RAG 응답방식 판단 (PROMPT_ID: 27) → JSON string

    Args:
        question: 현재 사용자 질문
        messages: 전체 대화 메시지 목록 (멀티턴 컨텍스트용, 최근 5턴 포함)
    """
    multiturn_question = _build_multiturn_question(question, messages or [])
    return await _call_generation(multiturn_question, _RAG_NORAG_JUDGMENT_PROMPT_ID)


_REFORM_QUERY_PROMPT_ID = 17  # 쿼리재구성 (re-query)


async def deepserver_reform_query(question: str) -> str:
    """
    쿼리 재구성 - /rag/re-query 호출
    Returns: 재구성된 질의 문자열
    """
    result = await _call_requery(question, _REFORM_QUERY_PROMPT_ID)
    return result if result else question


async def deepserver_expand_query(question: str) -> List[str]:
    """
    쿼리 확장 - /rag/generation + Prompt 2
    Returns: 확장 쿼리 리스트
    """
    logger.info(f"[deepserver_expand_query] {question}")
    output = await _call_generation(question, _EXPAND_PROMPT_ID, task="")
    try:
        parsed = json.loads(output)
        queries = parsed.get("query", []) if isinstance(parsed, dict) else parsed
        return queries if isinstance(queries, list) else []
    except (json.JSONDecodeError, AttributeError):
        return []


async def deepserver_extract_triples(question: str) -> List[Dict[str, Any]]:
    """
    일반 트리플 추출 - /rag/generation + Prompt 18
    Returns: [{"Subject": str, "Predicate": str, "Object": str}, ...]
    """
    output = await _call_generation(question, _TRIPLE_PROMPT_ID, task="")
    try:
        parsed = json.loads(output)
        triples = parsed.get("triples", []) if isinstance(parsed, dict) else []
        return triples if isinstance(triples, list) else []
    except (json.JSONDecodeError, AttributeError):
        return []


async def deepserver_extract_comparison_attributes(question: str) -> List[str]:
    """
    비교 속성 추출 - /rag/generation + Prompt 19
    Returns: ["지원 대상", "지원 금액", ...]
    """
    output = await _call_generation(question, _COMP_ATTR_PROMPT_ID, task="")
    try:
        attributes = json.loads(output)
        return attributes if isinstance(attributes, list) else []
    except (json.JSONDecodeError, AttributeError):
        return []


async def deepserver_extract_comparison_triples(question: str) -> List[Dict[str, Any]]:
    """
    비교 트리플 추출 - /rag/generation + Prompt 20
    Returns: [{"Subject": str, "Predicate": str, "Object": str}, ...]
    """
    output = await _call_generation(question, _COMP_TRIPLE_PROMPT_ID, task="")
    try:
        parsed = json.loads(output)
        triples = parsed.get("triples", []) if isinstance(parsed, dict) else []
        return triples if isinstance(triples, list) else []
    except (json.JSONDecodeError, AttributeError):
        return []
