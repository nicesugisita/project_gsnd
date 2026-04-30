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
        # 기초연금은 국가 기본 소득보장이라 노인 혜택 질의에서 다른 시군 사업.hwpx 들보다
        # 먼저 설명되는 것이 자연스럽다. apply_policy 에서 별도 1티어로 올린다.
        return frozenset({"elderly_benefits"}), (
            "기초연금",
            "기초 연금",
            "노인맞춤돌봄",
            "노인 맞춤돌봄",
            "맞춤돌봄",
            "돌봄서비스",
        )

    return frozenset(), ()


def augment_okms_dual_query(
    user_message: str,
    vector_q: str,
    keyword_q: str,
) -> Tuple[str, str]:
    """Mariner Group A 검색 직전 (vector, keyword) 보강.

    트리플이 비어 있던 행을 건드리며 keyword 레그만 채우면, 수집 루프의 tri_built
    인덱스와 맞지 않아 키워드 결과가 버려질 수 있으므로, keyword 보강은 기존
    트리플 문자열이 있을 때만 한다. 빈 트리플 레그 보강은 `policy_extra_okms_searches`.
    """
    tags, kws = resolve_policy_boost_keywords(user_message)
    vec = (vector_q or "").strip()
    kw = (keyword_q or "").strip()
    if not tags:
        return vec, kw

    low_vec = vec.casefold()
    if "elderly_benefits" in tags:
        for needle in ("기초연금", "노인맞춤돌봄"):
            if needle.casefold() not in low_vec:
                vec = f"{vec} {needle}".strip()
                low_vec = vec.casefold()
    elif "implant" in tags:
        if "임플란트" not in low_vec:
            vec = f"{vec} 임플란트".strip()
            low_vec = vec.casefold()
    elif "low_income" in tags:
        for needle in ("생계급여", "의료급여"):
            if needle.casefold() not in low_vec:
                vec = f"{vec} {needle}".strip()
                low_vec = vec.casefold()

    if kw and kws:
        kw_cf = kw.casefold()
        extra = [
            t
            for t in kws
            if t.strip() and t.strip().casefold() not in kw_cf
        ]
        if extra:
            kw = f"{kw} {' '.join(extra)}".strip()

    return vec, kw


def policy_extra_okms_searches(user_message: str, reformed_query: str) -> List[Tuple[str, str]]:
    """정책 태그별 OKMS Group A 추가 검색 (vector, keyword) 쌍 — 빈 트리플·약한 검색 보강."""
    tags, _ = resolve_policy_boost_keywords(user_message)
    rq = (reformed_query or "").strip()
    if not tags or not rq:
        return []
    if "elderly_benefits" in tags:
        return [
            (f"{rq} 기초연금 안내", "기초연금"),
            (f"{rq} 노인맞춤돌봄", "노인맞춤돌봄"),
        ]
    if "implant" in tags:
        return [(f"{rq} 임플란트 지원", "임플란트")]
    if "low_income" in tags:
        return [(f"{rq} 생계급여 의료급여", "생계급여 의료급여")]
    return []


def policy_supplement_welfare_queries(user_message: str) -> List[str]:
    """search 의도 WELFARE_CENTER/TEL 검색에 추가로 던질 짧은 쿼리."""
    tags, _ = resolve_policy_boost_keywords(user_message)
    if "elderly_benefits" in tags:
        return ["기초연금", "노인맞춤돌봄"]
    if "implant" in tags:
        return ["임플란트", "치과"]
    if "low_income" in tags:
        return ["생계급여", "의료급여"]
    return []


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
    _basic_pen_cf = frozenset({"기초연금".casefold(), "기초 연금".casefold()})

    def _hit_count(blob: str) -> int:
        return sum(1 for kw in kws_cf if kw in blob)

    def _elderly_sort_key(blob: str) -> Tuple[int, int]:
        """(기초연금·기초 연금 포함 여부, 나머지 부스트 키 적중 수)."""
        has_basic = any(p in blob for p in _basic_pen_cf)
        sec = sum(
            1
            for kw in kws_cf
            if kw not in _basic_pen_cf and kw in blob
        )
        return (1 if has_basic else 0, sec)

    blobs = [_document_policy_match_blob(d) for d in docs]

    if "elderly_benefits" in tags:
        max_basic, max_sec = 0, 0
        for b in blobs:
            prim, sec = _elderly_sort_key(b)
            max_basic = max(max_basic, prim)
            max_sec = max(max_sec, sec)
        if max_basic == 0 and max_sec == 0:
            logger.info("[%s] tags=%s — 매칭 문서 없음, 순서 유지", log_prefix, sorted(tags))
            return docs
        scored: List[Tuple[int, int, float, str, Dict[str, Any]]] = []
        for d, blob in zip(docs, blobs):
            prim, sec = _elderly_sort_key(blob)
            w = float(d.get("WEIGHT", 0) or 0)
            cid = str(d.get("CHUNK_ID", "") or d.get("ID", "") or "")
            scored.append((prim, sec, w, cid, d))
        scored.sort(key=lambda t: (-t[0], -t[1], -t[2], t[3]))
        reordered = [t[4] for t in scored]
        logger.info(
            "[%s] tags=%s — 노인혜택: 기초연금 1티어 후 나머지 키 조합 (boost_keys=%s)",
            log_prefix,
            sorted(tags),
            list(boost_keywords),
        )
        return reordered

    max_hits = max((_hit_count(b) for b in blobs), default=0)
    if max_hits == 0:
        logger.info("[%s] tags=%s — 매칭 문서 없음, 순서 유지", log_prefix, sorted(tags))
        return docs

    scored_kw: List[Tuple[int, float, str, Dict[str, Any]]] = []
    for d, blob in zip(docs, blobs):
        hits = _hit_count(blob)
        w = float(d.get("WEIGHT", 0) or 0)
        cid = str(d.get("CHUNK_ID", "") or d.get("ID", "") or "")
        scored_kw.append((hits, w, cid, d))

    scored_kw.sort(key=lambda t: (-t[0], -t[1], t[2]))

    reordered = [t[3] for t in scored_kw]
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
