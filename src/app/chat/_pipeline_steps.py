"""채팅 파이프라인 공통 단계 함수

routes.py(non-streaming)와 streaming.py(streaming) 양쪽에서 호출합니다.
각 함수는 순수하게 결과를 반환하며, 상태 메시지 전송은 호출자가 담당합니다.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from app.chat.service import unified_preprocess
from app.shared.utils.keyword_extractor import extract_nouns
from app.chat.infra.llm.judgment import pre_check
from app.chat.lifecycle import check_lifecycle
from app.chat.sigun import check_sigun, check_out_of_scope_region

logger = logging.getLogger(__name__)


@dataclass
class PreCheckResult:
    use_rag: bool = True
    clarification_question: str = ""
    elapsed: float = 0.0


@dataclass
class SigunCheckResult:
    filters: Optional[List[str]] = None
    need_clarify: bool = False
    ask_message: str = ""


@dataclass
class PreprocessResult:
    query: str = ""
    intent: str = "general"
    intent_reason: str = ""
    reformed_query: str = ""
    expanded_queries: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    elapsed: float = 0.0
    search_target: Optional[str] = None
    policy_priority_tag: Optional[str] = None
    detail_requested: bool = False


async def run_pre_check(
    user_message: str,
    messages: list,
    is_clarification: bool,
) -> PreCheckResult:
    """[1단계] RAG 사용 여부 + 되묻기 필요 여부 판단. is_clarification이면 스킵."""
    if is_clarification:
        return PreCheckResult()
    _t = time.monotonic()
    result = await pre_check(user_message, messages)
    elapsed = round(time.monotonic() - _t, 3)
    logger.info("[TIMING] pre_check: %.3fs", elapsed)
    return PreCheckResult(
        use_rag=result["use_rag"],
        clarification_question=result.get("clarification_question", ""),
        elapsed=elapsed,
    )


def run_early_sigun_check(
    user_message: str,
    messages: list,
    use_rag: bool,
    is_clarification: bool,
) -> Optional[SigunCheckResult]:
    """[2단계] 되묻기 답변 직후 시군 조기 확인 (query_recreation LLM 호출 전).

    is_clarification=True이고 use_rag=True인 경우에만 실행하며,
    해당하지 않으면 None을 반환한다.
    """
    if not (is_clarification and use_rag):
        return None
    filters, need_ask, ask_msg = check_sigun(user_message, messages)
    return SigunCheckResult(filters=filters or None, need_clarify=need_ask, ask_message=ask_msg)


def run_out_of_scope_check(user_message: str, use_rag: bool) -> tuple[bool, str]:
    """[3단계] 경상남도 외 지역 여부 확인. use_rag=False면 항상 (False, '') 반환."""
    if not use_rag:
        return False, ""
    return check_out_of_scope_region(user_message)


def run_sigun_check(
    user_message: str,
    messages: list,
    use_rag: bool,
    pre_resolved: Optional[List[str]],
) -> Optional[SigunCheckResult]:
    """[4단계] 시군 필터 확정. 이미 확정됐거나 RAG 비활성이면 None 반환."""
    if not use_rag or pre_resolved is not None:
        return None
    filters, need_ask, ask_msg = check_sigun(user_message, messages)
    return SigunCheckResult(filters=filters or None, need_clarify=need_ask, ask_message=ask_msg)


def build_preprocess_skip_unified_recommended_question(user_message: str) -> PreprocessResult:
    """추천 질문 후속: 통합 전처리 LLM 없이 guide_recommend RAG에 필요한 최소 필드만 구성."""
    try:
        kw = extract_nouns(user_message)
    except Exception:
        kw = []
    q = (user_message or "").strip()
    expanded = [q] if q else []
    return PreprocessResult(
        query=q,
        intent="guide_recommend",
        intent_reason="recommended_question_skip_unified",
        reformed_query=q,
        expanded_queries=expanded,
        keywords=list(kw) if kw else [],
        elapsed=0.0,
        search_target=None,
        policy_priority_tag=None,
    )


async def run_unified_preprocess(
    user_message: str,
    messages: Optional[list],
    use_rag: bool,
) -> PreprocessResult:
    """[5단계] 통합 전처리: 의도 분류, 쿼리 개선, 확장, 키워드 추출."""
    _t = time.monotonic()
    result = await unified_preprocess(user_message, messages, use_rag=use_rag)
    elapsed = round(time.monotonic() - _t, 3)
    logger.info("[TIMING] unified_preprocess: %.3fs", elapsed)
    return PreprocessResult(
        query=result.get("query", user_message),
        intent=result.get("intent", "general"),
        intent_reason=result.get("intent_reason", ""),
        reformed_query=result.get("reformed_query", "") or user_message,
        expanded_queries=result.get("expanded_queries") or [],
        keywords=result.get("keywords") or [],
        elapsed=elapsed,
        search_target=result.get("search_target"),
        policy_priority_tag=result.get("policy_priority_tag"),
        detail_requested=bool(result.get("detail_requested", False)),
    )


def run_lifecycle_check(user_message: str, messages: list, use_rag: bool, intent: str) -> None:
    """[6단계] guide_recommend 전용 생애주기 확인 (로그만 기록, 제어 흐름에 영향 없음)."""
    if not (use_rag and intent == "guide_recommend"):
        return
    lifecycle, need_clarify, _ = check_lifecycle(user_message, messages)
    if need_clarify:
        logger.info("[LifecycleCheck] 생애주기 미확인 → 되묻기 없이 진행")
    else:
        logger.info("[LifecycleCheck] 생애주기 확인: '%s'", lifecycle)
