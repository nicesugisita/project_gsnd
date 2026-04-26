"""
검색 결과 충분성 판정 서비스

각 문서를 개별적으로 LLM에 병렬 요청하여 적합성을 판단합니다.
1건이라도 sufficient=true이면 전체를 충분으로 판정합니다.
모든 문서가 sufficient=false일 때만 다음 컬렉션 검색으로 진행합니다.
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from core.config import Config
from core.constants import CHAT_COMPLETIONS_ENDPOINT
from utils.prompt_loader import load_retrieval_sufficiency_judgment_prompt

logger = logging.getLogger(__name__)

_MAX_DOCS_FOR_JUDGMENT = 5
_MAX_SNIPPET_CHARS = 1200


# ============================================================
# httpx 싱글턴 클라이언트
# ============================================================

_http_client: Optional[httpx.AsyncClient] = None
_JUDG_POOL_LIMITS = httpx.Limits(max_keepalive_connections=5, keepalive_expiry=30)
_POOL_RETRY_ERRORS = (RuntimeError, httpx.RemoteProtocolError)


def _get_http_client() -> httpx.AsyncClient:
    """모듈 레벨 httpx 클라이언트 (재사용, lazy 초기화)"""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=Config.LLM_API_TIMEOUT, limits=_JUDG_POOL_LIMITS)
    return _http_client


async def _reset_http_client():
    """커넥션 풀 오류 시 싱글턴 폐기 — 열린 소켓을 닫고 재생성"""
    global _http_client
    old = _http_client
    _http_client = None
    if old and not old.is_closed:
        await old.aclose()


# ============================================================
# 문서 포맷팅
# ============================================================

def _truncate_text(text: str, limit: int = _MAX_SNIPPET_CHARS) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def _is_welfare_doc(doc: Dict[str, Any]) -> bool:
    return any(
        str(doc.get(key, "") or "").strip()
        for key in ("FACILITY_NAME", "FACILITY_TYPE", "ADDRESS")
    )


def _format_doc_for_judgment(doc: Dict[str, Any], index: int) -> str:
    if _is_welfare_doc(doc):
        lines = [
            f"[문서 {index}]",
            f"- 시설명: {str(doc.get('FACILITY_NAME', '') or '').strip() or '정보 없음'}",
            f"- 시설유형: {str(doc.get('FACILITY_TYPE', '') or '').strip() or '정보 없음'}",
            f"- 주소: {str(doc.get('ADDRESS', '') or '').strip() or '정보 없음'}",
        ]
        for field, label in (("TEL", "전화"), ("HOMEPAGE", "홈페이지")):
            value = str(doc.get(field, "") or "").strip()
            if value:
                lines.append(f"- {label}: {value}")
        return "\n".join(lines)

    name = (
        str(doc.get("NAME", "") or "").strip()
        or str(doc.get("BUSINESS_NAME", "") or "").strip()
        or str(doc.get("ORG_NM", "") or "").strip()
        or str(doc.get("CHUNK_ID", "") or "").strip()
        or "문서"
    )
    snippet = _truncate_text(
        str(doc.get("CHUNK_PATH", "") or doc.get("CONTENT", "") or "").strip()
    )
    lines = [
        f"[문서 {index}]",
        f"- 문서명: {name}",
    ]
    for field, label in (
        ("SIGUN", "지역"),
        ("YEAR", "연도"),
        ("COMPLI_DT", "기준일"),
        ("DEPARTMENT", "담당부서"),
        ("APPLICATION_PERIOD", "신청기간"),
        ("PURPOSE", "목적"),
        ("TEL", "연락처"),
    ):
        value = str(doc.get(field, "") or "").strip()
        if value:
            lines.append(f"- {label}: {value}")
    if snippet:
        lines.append(f"- 내용: {snippet}")
    return "\n".join(lines)


# ============================================================
# 단일 문서 적합성 판단
# ============================================================

async def _judge_single_doc_sufficiency(
    user_question: str,
    doc: Dict[str, Any],
    doc_index: int,
    prompt_template: str,
) -> tuple[int, bool, str]:
    """단일 문서의 적합성을 LLM으로 판단.

    Returns:
        (문서 인덱스, sufficient 여부, reason). 실패 시 (index, False, "error") 반환.
    """
    doc_text = _format_doc_for_judgment(doc, doc_index + 1)
    user_content = f"[질문]\n{user_question}\n\n[검색 결과]\n{doc_text}"

    api_endpoint = f"{Config.RELEVANCE_LLM_API_URL}{CHAT_COMPLETIONS_ENDPOINT}"
    payload = {
        "model": Config.RELEVANCE_LLM_MODEL_NAME,
        "messages": [
            {"role": "system", "content": prompt_template},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "seed": 1,
        "frequency_penalty": 0.1,
        "repetition_penalty": 1.1,
        "top_k": 1.0,
        "top_p": 1.0,
        "max_completion_tokens": 512,
        "response_format": {"type": "json_object"},
    }

    try:
        try:
            client = _get_http_client()
            response = await client.post(api_endpoint, json=payload)
        except _POOL_RETRY_ERRORS:
            logger.warning(f"[RetrievalJudgment] #{doc_index + 1} 커넥션 풀 오류 → 재시도")
            await _reset_http_client()
            client = _get_http_client()
            response = await client.post(api_endpoint, json=payload)

        if response.status_code == 200:
            result = response.json()
            if "choices" in result and len(result["choices"]) > 0:
                msg = result["choices"][0].get("message", {})
                content = msg.get("content")
                if content is None:
                    content = msg.get("reasoning_content")

                if not content:
                    logger.warning(f"[RetrievalJudgment] #{doc_index + 1} content 비어있음 → 부족")
                    return doc_index, False, "empty_content"

                parsed = json.loads(content.strip())
                sufficient = bool(parsed.get("sufficient", False))
                reason = str(parsed.get("reason", "") or "").strip()
                return doc_index, sufficient, reason

        logger.warning(f"[RetrievalJudgment] #{doc_index + 1} HTTP 오류: status={response.status_code} → 부족")
        return doc_index, False, "http_error"

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"[RetrievalJudgment] #{doc_index + 1} JSON 파싱 실패: {e} → 부족")
        return doc_index, False, "json_parse_error"
    except httpx.TimeoutException:
        logger.warning(f"[RetrievalJudgment] #{doc_index + 1} 타임아웃 → 부족")
        return doc_index, False, "timeout"
    except Exception as e:
        logger.warning(f"[RetrievalJudgment] #{doc_index + 1} LLM 호출 실패: {e} → 부족")
        return doc_index, False, "judgment_error"


# ============================================================
# 적합성 판정 (공개 인터페이스)
# ============================================================

async def retrieval_sufficiency_judgment(
    user_question: str,
    intent: str,
    collection_name: str,
    docs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    각 문서를 개별 병렬 LLM 호출로 적합성 판정합니다.

    - 1건이라도 sufficient=true → 전체 sufficient=true
    - 모든 문서가 sufficient=false → 전체 sufficient=false → 다음 컬렉션 검색 진행

    Returns:
        {"sufficient": bool, "reason": str}
    """
    # TODO: 임시 강제 활성화
    # Config.RETRIEVAL_JUDGMENT_ENABLED 값과 무관하게 실제 LLM 판정을 수행한다.
    if not Config.RETRIEVAL_JUDGMENT_ENABLED:
        logger.info("[RetrievalJudgment] 설정 비활성화 감지, 임시 강제 활성화로 계속 진행")

    if not docs:
        logger.info(
            "[RetrievalJudgment] intent=%s collection=%s -> empty docs",
            intent, collection_name,
        )
        return {"sufficient": False, "reason": "empty_docs"}

    prompt = load_retrieval_sufficiency_judgment_prompt()
    if not prompt:
        logger.warning("[RetrievalJudgment] 프롬프트 로드 실패 → 부족 처리")
        return {"sufficient": False, "reason": "prompt_not_found"}

    target_docs = docs[:_MAX_DOCS_FOR_JUDGMENT]
    logger.info(
        "[RetrievalJudgment] 적합성 판단 시작: %d건, intent=%s, collection=%s",
        len(target_docs),
        intent,
        collection_name,
    )
    # 문서명/청크 식별자 노출 방지: target_docs 상세 로그 비활성화
    # logger.info(
    #     f"[RetrievalJudgment] [docs] "
    #     f"{', '.join(str(doc.get('NAME', '') or '').strip() or str(doc.get('CHUNK_ID', '') or '').strip() or '?' for doc in target_docs)}"
    # )

    # 병렬 판단
    t0 = time.monotonic()
    tasks = [
        _judge_single_doc_sufficiency(user_question, doc, i, prompt)
        for i, doc in enumerate(target_docs)
    ]
    results = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - t0
    logger.info(f"[RetrievalJudgment] LLM 판단 완료: {len(target_docs)}건, {elapsed:.3f}s")

    # 결과 집계: 1건이라도 true면 충분
    sufficient_docs = []
    insufficient_docs = []
    for doc_index, is_sufficient, reason in results:
        doc_name = str(target_docs[doc_index].get("NAME", "") or "").strip() or f"#{doc_index + 1}"
        if is_sufficient:
            sufficient_docs.append(doc_name)
        else:
            insufficient_docs.append(doc_name)
        logger.info(
            f"[RetrievalJudgment] #{doc_index + 1} '{doc_name}' → "
            f"{'충분' if is_sufficient else '부족'} (reason={reason})"
        )

    overall_sufficient = len(sufficient_docs) > 0

    if overall_sufficient:
        overall_reason = f"적합 문서 {len(sufficient_docs)}건: {sufficient_docs}"
    else:
        overall_reason = f"전체 {len(target_docs)}건 부적합"

    logger.info(
        f"[RetrievalJudgment] 최종: sufficient={overall_sufficient}, "
        f"적합={len(sufficient_docs)}건, 부적합={len(insufficient_docs)}건"
    )

    return {"sufficient": overall_sufficient, "reason": overall_reason}
