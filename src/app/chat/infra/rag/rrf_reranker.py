"""
RRF(Reciprocal Rank Fusion) 재순위화 — 이미 정렬된 N개 문서 리스트를 rank로 융합한다.

Cormack 2009 원논문에 가까운 "순수" RRF. 입력 리스트가 어떤 검색기에서 왔는지,
몇 개인지는 알 필요가 없다 — 그저 "정렬된 리스트들"만 받아 각 리스트 내 1-based
rank로 점수를 계산한다.

    RRF(d) = Σ_{i: d ∈ list_i}  w_i / (RRF_K + rank_i(d))

RRF_K는 60으로 고정한다(원논문 권장값).

동일 문서를 여러 리스트에서 같은 키로 식별하기 위해 ``key`` 함수가 필요하다.
기본은 CHUNK_ID → ID → id(doc) 순으로 폴백한다. 키가 같으면 같은 문서로 본다.

사용 예 (가변인자 — Java varargs ``...`` 와 동일):
    rerank_by_rrf(list_a)                              # 1개
    rerank_by_rrf(list_a, list_b)                      # 2개
    rerank_by_rrf(list_a, list_b, list_c, list_d)      # 4개
    rerank_by_rrf(*my_lists)                           # 리스트의 리스트 언패킹
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


RRF_K: int = 60

# 융합 결과 점수가 채워지는 키
SCORE_RRF: str = "rrf_score"


def rerank_by_rrf(
    *docs_lists: List[Dict[str, Any]],
    top_k: int = 0,
    weights: Optional[Sequence[float]] = None,
    key: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> List[Dict[str, Any]]:
    """이미 정렬된 N개의 문서 리스트를 Reciprocal Rank Fusion으로 융합 정렬한다.

    Args:
        *docs_lists: 가변 개수의 이미 정렬된 문서 리스트. 각 리스트의 0번 원소가
            그 리스트의 1등이라고 가정한다. 빈 리스트는 무시된다.
        top_k: 반환할 최대 문서 수. 0이면 전체.
        weights: 각 리스트의 가중치. None이면 전부 1.0. 길이는 ``docs_lists`` 와
            같아야 한다.
        key: 문서 식별 함수. 동일 문서가 여러 리스트에 나타날 때 같은 키를 돌려줘야
            한다. None이면 CHUNK_ID → ID → id(doc) 순으로 폴백.

    Returns:
        RRF 점수 내림차순으로 정렬된 새 리스트. 각 문서가 처음 발견된 인스턴스를
        대표로 사용하고, 그 dict의 SCORE_RRF 키에 융합 점수를 채워 넣는다.
        ``docs_lists`` 가 비어 있거나 전부 빈 리스트면 빈 리스트를 돌려준다.
    """
    if not docs_lists:
        return []

    if weights is None:
        weights = [1.0] * len(docs_lists)
    elif len(weights) != len(docs_lists):
        raise ValueError(
            f"weights 길이({len(weights)})가 docs_lists 길이({len(docs_lists)})와 다릅니다"
        )

    key_fn = key or _default_key

    # 키 기준 누적 — 첫 등장 인스턴스를 대표로 보존하고 같은 키 등장 시 RRF만 더한다
    accumulated: Dict[Any, Dict[str, Any]] = {}
    scores: Dict[Any, float] = {}

    for list_idx, ranked in enumerate(docs_lists):
        if not ranked:
            continue
        w = float(weights[list_idx])
        for rank, doc in enumerate(ranked, start=1):
            k = key_fn(doc)
            if k not in accumulated:
                accumulated[k] = doc
                scores[k] = 0.0
            scores[k] += w / (RRF_K + rank)

    if not accumulated:
        return []

    for k, doc in accumulated.items():
        doc[SCORE_RRF] = scores[k]

    fused = sorted(
        accumulated.values(),
        key=lambda d: float(d.get(SCORE_RRF, 0.0) or 0.0),
        reverse=True,
    )

    logger.debug(
        "[RRF] k=%d lists=%d weights=%s in=%s out=%d top=%.6f bottom=%.6f",
        RRF_K, len(docs_lists), list(weights),
        [len(rl) for rl in docs_lists], len(fused),
        fused[0][SCORE_RRF], fused[-1][SCORE_RRF],
    )

    if 0 < top_k < len(fused):
        fused = fused[:top_k]

    return fused


def _default_key(doc: Dict[str, Any]) -> Any:
    """기본 식별 키 — CHUNK_ID > ID > 객체 id."""
    chunk_id = doc.get("CHUNK_ID")
    if chunk_id:
        return ("CHUNK_ID", chunk_id)
    doc_id = doc.get("ID")
    if doc_id:
        return ("ID", doc_id)
    return ("obj", id(doc))
