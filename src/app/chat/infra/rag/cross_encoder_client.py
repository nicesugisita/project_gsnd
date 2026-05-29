"""Cross-Encoder 리랭커 클라이언트 — `cross_encoder/` FastAPI 서비스(/rerank) 호출.

RRF(rank 융합)와 달리 (query, 문서본문) 쌍마다 절대 관련성 점수를 받아 정렬한다.
풀 구분·WEIGHT 스케일 보정이 필요 없고, 동일 입력에 결정적이다.

httpx 싱글턴 관리는 `app/chat/infra/llm/client.py` 패턴을 그대로 따른다(모듈 전역
lazy 초기화 — 호출부 함수 시그니처를 건드리지 않는다).

서비스 다운/타임아웃/빈응답 시 WEIGHT 내림차순 정렬로 graceful degrade 한다.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import Config
from app.chat.infra.rag.document import _get_document_name, _get_document_snippet

logger = logging.getLogger(__name__)

# cross-encoder 점수가 채워지는 키
SCORE_CE: str = "cross_encoder_score"

_ce_client: Optional[httpx.AsyncClient] = None
_CE_POOL_LIMITS = httpx.Limits(max_keepalive_connections=10, keepalive_expiry=30)


def _get_ce_client() -> httpx.AsyncClient:
    """cross-encoder용 모듈 레벨 httpx 클라이언트 (재사용, lazy 초기화)."""
    global _ce_client
    if _ce_client is None or _ce_client.is_closed:
        _ce_client = httpx.AsyncClient(
            timeout=Config.CROSS_ENCODER_TIMEOUT, limits=_CE_POOL_LIMITS
        )
    return _ce_client


async def _reset_ce_client() -> None:
    """커넥션 풀 오류·shutdown 시 싱글턴 폐기 — 열린 소켓을 닫고 재생성 가능 상태로."""
    global _ce_client
    old = _ce_client
    _ce_client = None
    if old and not old.is_closed:
        await old.aclose()


def _doc_text(doc: Dict[str, Any]) -> str:
    """리랭킹에 보낼 문서 텍스트 — 문서명 + 본문 스니펫을 MAX_DOC_CHARS로 절단."""
    name = _get_document_name(doc)
    snippet = _get_document_snippet(doc)
    text = f"{name}\n{snippet}" if name else snippet
    return text[: Config.CROSS_ENCODER_MAX_DOC_CHARS]


def _weight_sorted(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """폴백 정렬 — 기존 WEIGHT 내림차순."""
    return sorted(docs, key=lambda d: float(d.get("WEIGHT", 0) or 0), reverse=True)


async def rerank_by_cross_encoder(
    query: str, docs: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """후보 문서들을 cross-encoder 관련성 점수 내림차순으로 정렬한다.

    Args:
        query: 리랭킹 기준 쿼리(검색에 쓴 reformed_query).
        docs: 후보 문서 리스트(이미 dedup된 단일 풀). 순서 무관.

    Returns:
        cross-encoder 점수 내림차순으로 정렬된 docs. 각 doc의 ``SCORE_CE`` 키에 점수를
        채운다. 서비스 오류·타임아웃·빈응답 시 WEIGHT 내림차순 정렬로 폴백한다.
        ``docs`` 가 비면 빈 리스트.
    """
    if not docs:
        return []

    documents = [_doc_text(d) for d in docs]
    payload = {"query": query, "documents": documents, "top_n": len(docs)}

    try:
        client = _get_ce_client()
        resp = await client.post(
            f"{Config.CROSS_ENCODER_URL.rstrip('/')}/rerank", json=payload
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 폴백
        logger.warning(
            "[CrossEncoder] /rerank 호출 실패 → WEIGHT 정렬 폴백: %s (in=%d)",
            exc, len(docs),
        )
        return _weight_sorted(docs)

    if not results:
        logger.warning("[CrossEncoder] 빈 응답 → WEIGHT 정렬 폴백 (in=%d)", len(docs))
        return _weight_sorted(docs)

    # results[].index 로 원본 doc 매핑 + 점수 기록. 응답이 이미 score 내림차순이지만,
    # 누락 index 방어를 위해 명시적으로 재정렬한다.
    ranked: List[Dict[str, Any]] = []
    for r in results:
        idx = r.get("index")
        if idx is None or not (0 <= idx < len(docs)):
            continue
        doc = docs[idx]
        doc[SCORE_CE] = float(r.get("relevance_score", 0.0) or 0.0)
        ranked.append(doc)

    if not ranked:
        logger.warning("[CrossEncoder] 유효 결과 0건 → WEIGHT 정렬 폴백 (in=%d)", len(docs))
        return _weight_sorted(docs)

    ranked.sort(key=lambda d: float(d.get(SCORE_CE, 0.0) or 0.0), reverse=True)
    logger.debug(
        "[CrossEncoder] rerank in=%d out=%d top=%.4f bottom=%.4f",
        len(docs), len(ranked), ranked[0][SCORE_CE], ranked[-1][SCORE_CE],
    )
    return ranked
