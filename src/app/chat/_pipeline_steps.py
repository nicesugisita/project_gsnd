"""채팅 파이프라인 공통 단계 함수

routes.py(non-streaming)와 streaming.py(streaming) 양쪽에서 호출합니다.
각 함수는 순수하게 결과를 반환하며, 상태 메시지 전송은 호출자가 담당합니다.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from app.chat.service import unified_preprocess
from app.chat.preprocessing import classify_query_tags
from app.shared.utils.keyword_extractor import extract_nouns
from app.chat.infra.llm.judgment import pre_check
from app.chat.lifecycle import check_lifecycle
from app.chat.sigun import check_sigun, check_out_of_scope_region
from app.chat.topic_clarify import check_topic_clarification, last_assistant_is_topic_ask

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
class TopicClarifyResult:
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
    lifecycle_tags: Optional[List[str]] = None
    household_tags: Optional[List[str]] = None
    detail_requested: bool = False
    # 작업 6: 배제 의도 분석 (rewrite 모드 unified_preprocess에서만 의미 있게 채워짐)
    exclusion_intent: str = "NONE"
    vector_query: str = ""
    must_not_keywords: List[str] = field(default_factory=list)
    anchor_entities: List[str] = field(default_factory=list)


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


def run_topic_clarify_check(
    original_user_question: str,
    messages: list,
    use_rag: bool,
    is_clarification: bool,
    *,
    current_user_message: str = "",
) -> Optional[TopicClarifyResult]:
    """[2.5단계] 정보량 0 입력에 대해 회차별 분야 되묻기.

    호출 케이스:
    1) 시군 확정 직후(is_clarification=True): 원질문(`original_user_question`)을
       기준으로 1회차 메시지 발동.
    2) topic_clarify 후속 turn(직전 assistant 가 topic_ask 마커): is_clarification
       이 False여도(메시지가 '?'로 안 끝남) 게이트 우회. 현재 user 메시지
       (`current_user_message`)를 기준으로 2회차/3회차/fallback 진행.

    `use_rag=False` 면 항상 None.
    회차에 cap이 없고, 풀 초과 시 fallback 메시지가 반복된다(`topic_clarify`).
    """
    if not use_rag:
        return None
    is_followup = last_assistant_is_topic_ask(messages)
    if not (is_clarification or is_followup):
        return None
    base_query = current_user_message if is_followup else original_user_question
    need_ask, ask_msg = check_topic_clarification(base_query, messages)
    if not need_ask:
        return None
    return TopicClarifyResult(need_clarify=True, ask_message=ask_msg)


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
    """[5단계] 통합 전처리: 의도 분류, 쿼리 개선, 확장, 키워드 추출.

    생애주기·정책 우선순위 태그 분류는 별도 분류기(classify_query_tags)로 분리돼 있어
    여기서 unified_preprocess와 asyncio.gather 로 병렬 호출한다. 분류기 lifecycle_tags가
    None이면(LLM/파싱 실패) 파이프라인이 기존 룰 기반 생애주기로 폴백하고, policy는 분류기
    내부에서 excludes 무력화 + 룰 fallback까지 마쳐 반환한다.
    """
    _t = time.monotonic()
    result, (lifecycle_tags, policy_priority_tag, household_tags) = await asyncio.gather(
        unified_preprocess(user_message, messages, use_rag=use_rag),
        classify_query_tags(user_message, messages),
    )
    elapsed = round(time.monotonic() - _t, 3)
    logger.info(
        "[TIMING] unified_preprocess(+tags): %.3fs | lifecycle=%s | policy=%s | household=%s",
        elapsed, lifecycle_tags, policy_priority_tag, household_tags,
    )
    return PreprocessResult(
        query=result.get("query", user_message),
        intent=result.get("intent", "general"),
        intent_reason=result.get("intent_reason", ""),
        reformed_query=result.get("reformed_query", "") or user_message,
        expanded_queries=result.get("expanded_queries") or [],
        keywords=result.get("keywords") or [],
        elapsed=elapsed,
        search_target=result.get("search_target"),
        policy_priority_tag=policy_priority_tag,
        lifecycle_tags=lifecycle_tags,
        household_tags=household_tags,
        detail_requested=bool(result.get("detail_requested", False)),
        exclusion_intent=result.get("exclusion_intent", "NONE"),
        vector_query=result.get("vector_query") or result.get("reformed_query") or user_message,
        must_not_keywords=result.get("must_not_keywords") or [],
        anchor_entities=result.get("anchor_entities") or [],
    )


def run_lifecycle_check(user_message: str, messages: list, use_rag: bool, intent: str) -> None:
    """[6단계] requires_lifecycle=True intent 전용 생애주기 확인.

    로그만 기록하며 제어 흐름에 영향 없음. intent 별 활성화 여부는
    `chat.intent_registry.IntentSpec.requires_lifecycle` 메타데이터로 관리.
    """
    from app.chat.intent_registry import lookup as _lookup_intent_spec

    if not use_rag or not _lookup_intent_spec(intent).requires_lifecycle:
        return
    lifecycle, need_clarify, _ = check_lifecycle(user_message, messages)
    if need_clarify:
        logger.info("[LifecycleCheck] 생애주기 미확인 → 되묻기 없이 진행")
    else:
        logger.info("[LifecycleCheck] 생애주기 확인: '%s'", lifecycle)
