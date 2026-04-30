"""질문군별 핵심 서비스 soft-priority — common/response_generator와 순환 없이 공유."""

from __future__ import annotations

import logging
from typing import Any, Dict, FrozenSet, List, Tuple

logger = logging.getLogger(__name__)


def _elderly_benefits_heuristic(message: str) -> bool:
    """노인·고령 대상 혜택/지원 질의 여부(키워드 휴리스틱)."""
    m = message.strip()
    if not m:
        return False
    age_or_elder = any(
        x in m
        for x in ("70", "칠십", "노인", "어르신", "부모", "고령")
    )
    benefit_ctx = any(
        x in m
        for x in ("혜택", "지원", "돌봄", "연금", "급여", "복지")
    )
    return age_or_elder and benefit_ctx


def resolve_policy_boost_keywords(message: str) -> Tuple[FrozenSet[str], Tuple[str, ...]]:
    """질문에 맞는 정책 태그와, 문서 매칭용 부스트 키워드 튜플을 반환한다.

    우선순위: 임플란트 > 저소득 > 노인혜택 (동시에 걸릴 때 고객 예시 기준).
    """
    msg = message.strip()
    if not msg:
        return frozenset(), ()

    if "임플란트" in msg:
        return frozenset({"implant"}), ("임플란트", "치과", "구강", "의료급여")

    if any(k in msg for k in ("저소득", "생계급여", "의료급여")):
        return frozenset({"low_income"}), ("생계급여", "의료급여", "저소득", "기초생활")

    if _elderly_benefits_heuristic(msg):
        return frozenset({"elderly_benefits"}), (
            "기초연금",
            "노인맞춤돌봄",
            "노인 맞춤돌봄",
            "맞춤돌봄",
            "돌봄서비스",
        )

    return frozenset(), ()


def _document_policy_match_blob(doc: Dict[str, Any]) -> str:
    """정책 키워드 매칭용 텍스트(소문자)."""
    parts: List[str] = []
    for k in (
        "NAME",
        "BUSINESS_NAME",
        "ORG_NM",
        "FACILITY_NAME",
        "PURPOSE",
        "CONTENT",
        "CHUNK_PATH",
        "APPLICATION_PERIOD",
        "FACILITY_TYPE",
    ):
        parts.append(str(doc.get(k, "") or ""))
    return " ".join(parts).casefold()


def apply_policy_priority_to_documents(
    user_message: str,
    docs: List[Dict[str, Any]],
    *,
    log_prefix: str = "PolicyBoost",
) -> List[Dict[str, Any]]:
    """검색 후 문서 목록에 질문군별 soft-priority를 적용해 재정렬한다.

    - 문서 본문/제목에 정책 키워드가 포함된 후보를 앞으로 올린다.
    - 어느 문서에도 키워드가 없으면 원 순서를 유지한다(불필요한 순서 뒤집음 방지).
    - 동점 시 CHUNK_ID/ID 문자열로 안정 정렬.
    """
    if not docs:
        return docs

    tags, boost_keywords = resolve_policy_boost_keywords(user_message)
    if not boost_keywords:
        return docs

    kws_cf = tuple(kw.casefold() for kw in boost_keywords if kw.strip())

    def _hit_count(blob: str) -> int:
        return sum(1 for kw in kws_cf if kw in blob)

    blobs = [_document_policy_match_blob(d) for d in docs]
    max_hits = max((_hit_count(b) for b in blobs), default=0)
    if max_hits == 0:
        logger.info("[%s] tags=%s — 매칭 문서 없음, 순서 유지", log_prefix, sorted(tags))
        return docs

    scored: List[Tuple[int, float, str, Dict[str, Any]]] = []
    for d, blob in zip(docs, blobs):
        hits = _hit_count(blob)
        w = float(d.get("WEIGHT", 0) or 0)
        cid = str(d.get("CHUNK_ID", "") or d.get("ID", "") or "")
        scored.append((hits, w, cid, d))

    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))

    reordered = [t[3] for t in scored]
    logger.info(
        "[%s] tags=%s max_hits=%d — 정책 우선 재정렬 적용 (boost_keys=%s)",
        log_prefix,
        sorted(tags),
        max_hits,
        list(boost_keywords),
    )
    return reordered


def soft_priority_instruction_for_prompt(user_question: str) -> str:
    """최종 LLM user 메시지에 붙일 짧은 우선순위 안내(문서 근거만).

    주의:
    - 특정 키워드 목록을 그대로 노출하면 모델이 '키워드 부재' 안내로 치우칠 수 있어
      태그만 전달하고, '있는 문서만 우선 설명'하도록 완화한다.
    """
    tags, _kws = resolve_policy_boost_keywords(user_question)
    if not tags:
        return ""
    return (
        "\n        [답변 우선순위]\n"
        "        아래 질문군에 해당합니다. retrieved_documents에서 관련성이 높은 문서를 "
        "기존 출력 규칙을 유지한 채 먼저 안내하세요.\n"
        "        관련 문서가 일부만 있으면 있는 것만 설명하고, "
        "키워드 부재 안내 문구로 답변을 대체하지 마세요.\n"
        "        문서에 없는 내용은 작성하지 마세요.\n"
        f"        - 태그: {', '.join(sorted(tags))}\n"
    )
