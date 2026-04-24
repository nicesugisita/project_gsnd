"""
문서 관련성 필터 — LLM 기반 무관 문서 제거

검색 결과 문서 목록에서 사용자 질문과 무관한 문서를 LLM으로 판단하여 제거합니다.
병렬 처리(asyncio.gather)로 latency를 최소화하고, 실패 시 문서를 유지하는 fail-safe 방식입니다.
httpx 싱글턴 클라이언트로 커넥션 풀을 재사용합니다.

전용 LLM API 클라이언트를 사용합니다 (RELEVANCE_LLM_API_URL).
"""

import asyncio
import json
import logging
import time
from typing import Dict, Any, List, Optional

import httpx

from core.config import Config
from core.constants import CHAT_COMPLETIONS_ENDPOINT

logger = logging.getLogger(__name__)

_RELEVANCE_SYSTEM_PROMPT = """
당신은 경상남도 복지 정보 검색 시스템의 문서 관련성 판단기입니다.
사용자의 질문과 검색된 문서를 비교하여, 해당 문서가 답변에 도움이 되는지 판단합니다.
Reasoning: Low

## 판단 규칙

### 무조건 제거 (relevant: false)
- 질문 주제와 완전히 다른 분야의 문서 (예: 육아 질문에 노인복지 문서)
- 질문에서 특정 연도를 명시했는데 문서가 다른 연도이고, 내용적 연관성도 없는 경우
- 사용자 지역이 제공된 경우, 문서의 지역(SIGUN)이 사용자 지역과 명백히 다른 문서 (단, 경상남도 전체 대상 문서는 유지)

### 무조건 유지 (relevant: true)
- 질문 주제와 직접 관련된 정책·제도·사업 정보가 포함된 문서
- 연도가 달라도 질문 주제와 동일한 사업·제도에 대한 문서 (제도 변경 이력, 연도별 비교 등에 유용)
- 지역이 명시되지 않은 일반적인 질문에 대한 문서
- 사용자 지역과 문서 지역이 일치하는 문서

### 대화 맥락 고려
- 이전 대화 이력이 함께 제공될 수 있습니다.
- 현재 질문만으로는 모호해 보여도, 이전 대화에서 주제가 이미 언급되었다면 해당 주제와 관련된 문서는 유지합니다.
- 예시: 이전에 "진주시 출산 지원금"을 질문한 후 "25년은요?"라고 물으면 → 출산 지원금 관련 문서는 유지

### 판단 우선순위
1. 주제 연관성이 가장 중요합니다. 주제가 맞으면 연도·지역이 약간 달라도 유지합니다.
2. 단, 시간 순서가 다르면 안됩니다. 예를 들어 `치매 걸렸는데 지원`일 경우 `치매 검사 관련 지원 문서`는 옳지 않습니다.
3. 사용자 지역이 명시된 경우 지역 일치를 고려하되, 주제가 맞으면 유지합니다.
4. 연도·지역 불일치는 주제와 무관할 때만 제거 사유가 됩니다.
5. 애매한 경우에는 유지(true)로 판단합니다 — 잘못 제거하는 것이 잘못 유지하는 것보다 나쁩니다.

## 응답 형식
반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 포함하지 마세요.

관련: {"relevant": true}
무관: {"relevant": false}"""


# ============================================================
# httpx 싱글턴 클라이언트
# ============================================================

_http_client: Optional[httpx.AsyncClient] = None
_REL_POOL_LIMITS = httpx.Limits(max_keepalive_connections=5, keepalive_expiry=30)
_POOL_RETRY_ERRORS = (RuntimeError, httpx.RemoteProtocolError)


def _get_http_client() -> httpx.AsyncClient:
    """모듈 레벨 httpx 클라이언트 (재사용, lazy 초기화)"""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=Config.LLM_API_TIMEOUT, limits=_REL_POOL_LIMITS)
    return _http_client


def _reset_http_client():
    """커넥션 풀 오류 시 싱글턴 폐기"""
    global _http_client
    _http_client = None


# ============================================================
# 메시지 빌더
# ============================================================

def _build_doc_messages(
    question: str,
    doc: Dict[str, Any],
    sigun_filters: Optional[List[str]] = None,
) -> Optional[List[Dict[str, str]]]:
    """단일 문서에 대한 [system, user] 메시지 배열 생성. 판단 불가 시 None."""
    doc_name = str(doc.get("NAME", "") or doc.get("BUSINESS_NAME", "") or "").strip()
    doc_content = str(doc.get("CHUNK_PATH", "") or doc.get("CONTENT", "") or "").strip()
    if not doc_content:
        doc_content = str(doc.get("PURPOSE", "") or "").strip()

    if not doc_name and not doc_content:
        return None

    parts = [f"질문: {question}"]
    if sigun_filters:
        parts.append(f"사용자 지역: {', '.join(sigun_filters)}")
    parts.append(f"문서: {doc_name} - {doc_content}")

    return [
        {"role": "system", "content": _RELEVANCE_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


# ============================================================
# 단일 문서 LLM 호출
# ============================================================

async def _judge_single_doc(
    question: str,
    doc: Dict[str, Any],
    doc_index: int,
    sigun_filters: Optional[List[str]] = None,
) -> tuple[int, bool]:
    """단일 문서의 관련성을 LLM으로 판단.

    Returns:
        (문서 인덱스, 관련 여부) 튜플. 판단 실패 시 True(유지) 반환.
    """
    msgs = _build_doc_messages(question, doc, sigun_filters=sigun_filters)
    if msgs is None:
        logger.debug(f"[RelevanceFilter] #{doc_index + 1} 문서 내용 없음 → 유지")
        return doc_index, True

    api_endpoint = f"{Config.RELEVANCE_LLM_API_URL}{CHAT_COMPLETIONS_ENDPOINT}"
    payload = {
        "model": Config.RELEVANCE_LLM_MODEL_NAME,
        "messages": msgs,
        "temperature": 0,
        "seed": 1,
        "frequency_penalty": 0.1,
        "repetition_penalty": 1.1,
        "top_k": 1.0,
        "top_p": 1.0,
        "max_completion_tokens": 512,
        "response_format": {"type": "json_object"},
    }

    # 요청 메시지 전체 로그
    # for m in msgs:
    #     logger.info(
    #         f"[RelevanceFilter] #{doc_index + 1} request [{m['role']}]: {m['content']}"
    #     )

    try:
        try:
            client = _get_http_client()
            response = await client.post(api_endpoint, json=payload)
        except _POOL_RETRY_ERRORS:
            logger.warning(f"[RelevanceFilter] #{doc_index + 1} 커넥션 풀 오류 → 재시도")
            _reset_http_client()
            client = _get_http_client()
            response = await client.post(api_endpoint, json=payload)

        if response.status_code == 200:
            result = response.json()
            if "choices" in result and len(result["choices"]) > 0:
                choice = result["choices"][0]
                msg = choice.get("message", {})
                content = msg.get("content")
                if content is None:
                    content = msg.get("reasoning_content")
                finish = choice.get("finish_reason", "")

                if not content:
                    logger.warning(
                        f"[RelevanceFilter] #{doc_index + 1} content 비어있음"
                        f" (finish_reason={finish}, keys={list(msg.keys())}) → 유지"
                    )
                    return doc_index, True

                parsed = json.loads(content.strip())
                is_relevant = bool(parsed.get("relevant", True))
                return doc_index, is_relevant

        return doc_index, True

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"[RelevanceFilter] #{doc_index + 1} JSON 파싱 실패: {e} → 유지")
        return doc_index, True
    except httpx.TimeoutException:
        logger.warning(f"[RelevanceFilter] #{doc_index + 1} 타임아웃 → 유지")
        return doc_index, True
    except Exception as e:
        logger.warning(f"[RelevanceFilter] #{doc_index + 1} LLM 호출 실패: {e} → 유지")
        return doc_index, True


# ============================================================
# 문서 관련성 필터 (공개 인터페이스)
# ============================================================

async def filter_irrelevant_docs(
    question: str,
    docs: List[Dict[str, Any]],
    sigun_filters: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """문서 목록에서 질문과 무관한 문서를 LLM으로 판단하여 제거.

    Args:
        question: 사용자 질문 (reformed_query — 대화 맥락이 반영된 검색 쿼리)
        docs: 검색 결과 문서 목록
        sigun_filters: 사용자 지역 필터

    Returns:
        관련 문서만 남긴 목록
    """
    _MAX_DOCS_FOR_FILTER = 10

    if not docs:
        return docs

    if len(docs) <= 1:
        logger.debug("[RelevanceFilter] 문서 1건 이하 → 필터 생략")
        return docs

    # 상위 10건만 판단 대상, 나머지 버림
    target_docs = docs[:_MAX_DOCS_FOR_FILTER]
    if len(docs) > _MAX_DOCS_FOR_FILTER:
        logger.info(f"[RelevanceFilter] ========================= docs: {docs}")
        logger.info(f"[RelevanceFilter] {len(docs)}건 중 상위 {_MAX_DOCS_FOR_FILTER}건만 판단, 나머지 {len(docs) - _MAX_DOCS_FOR_FILTER}건 제거")

    logger.info(
        f"[RelevanceFilter] 관련성 판단 시작: {len(target_docs)}건 문서, "
        f"질문: '{question[:50]}'"
    )

    # 병렬 판단 (asyncio.gather + 싱글턴 클라이언트로 커넥션 풀 재사용)
    t0 = time.monotonic()
    tasks = [
        _judge_single_doc(question, doc, i, sigun_filters=sigun_filters)
        for i, doc in enumerate(target_docs)
    ]
    results = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - t0
    logger.info(f"[RelevanceFilter] LLM 판단 완료  ====================== target_docs: {target_docs}")
    logger.info(f"[RelevanceFilter] LLM 판단 완료: {len(target_docs)}건, {elapsed:.3f}s")

    # 관련 문서만 필터링
    relevant_docs = []
    relevant_names = []
    removed_names = []
    for doc_index, is_relevant in results:
        doc_name = str(target_docs[doc_index].get("NAME", "") or "").strip() or f"#{doc_index + 1}"
        if is_relevant:
            relevant_docs.append(target_docs[doc_index])
            relevant_names.append(doc_name)
        else:
            removed_names.append(doc_name)

    # 결과 로그
    if relevant_names:
        logger.info(f"[RelevanceFilter] 유관 문서 {len(relevant_names)}건 유지: {relevant_names}")
        logger.info(f"[RelevanceFilter] 유관 문서 예시: {[relevant_names[i] for i in range(len(relevant_names))]}")
    if removed_names:
        logger.info(f"[RelevanceFilter] 무관 문서 {len(removed_names)}건 제거: {removed_names}")
        logger.info(f"[RelevanceFilter] 무관 문서 예시: {[removed_names[i] for i in range(len(removed_names))]}")
    if not removed_names:
        logger.info("[RelevanceFilter] 무관 문서 없음 — 전체 유지")

    logger.info(f"[RelevanceFilter] 필터 결과: {len(target_docs)}건 → {len(relevant_docs)}건")
    logger.info(f"[RelevanceFilter] 최종 문서: {relevant_names}")
    return relevant_docs
