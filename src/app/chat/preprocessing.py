"""통합 전처리 서비스 — 단일 LLM 호출로 4가지 작업 동시 수행"""

import json
import logging
from datetime import date
from typing import Any, Dict, List, Optional

from app.core.config import Config

from app.core.constants import ROLE_USER, ROLE_ASSISTANT
from app.chat.infra.rag.query_builder import (
    sanitize_disability_keywords,
    sanitize_disability_text,
)
from app.shared.utils.keyword_extractor import extract_nouns
from app.chat.infra.llm import call_llm_api
from app.chat.infra.llm.classifier_fallback import call_classifier_with_fallback
from app.shared.utils.prompt_loader import load_unified_preprocessing_prompt
from app.chat.routing import KEYWORD_BOOST_MAP, _apply_keyword_boost

logger = logging.getLogger(__name__)

# P3: intent_registry 가 단일 진실 공급원. 본 튜플은 하위 호환·import 편의를 위한 별칭.
from app.chat.intent_registry import get_intent_names as _get_intent_names

VALID_INTENTS = _get_intent_names()

SEARCH_TARGETS = frozenset({"admin_local_office", "welfare_facility", "ambiguous"})
POLICY_PRIORITY_TAGS = frozenset({"implant", "low_income", "elderly_benefits"})


def _raw_search_target_from_parsed(parsed: Dict[str, Any]) -> Any:
    """LLM JSON에서 search_target 읽기(스네이크/카멜·빈값·문자열 null 허용)."""
    order = ("search_target", "searchTarget")
    for key in order:
        v = parsed.get(key)
        if v is None:
            continue
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                continue
            low = stripped.lower()
            if low in ("null", "none"):
                continue
            return stripped
        return v
    return None


_FALLBACK = {
    "intent": "general",
    "intent_reason": "",
}

_MAX_HISTORY_MESSAGES = 6   # 최대 3턴(user+assistant 쌍)
_ASSISTANT_SUMMARY_LEN = 200


def _build_multiturn_input(user_query: str, messages: list) -> str:
    """이전 대화 이력을 [이전 대화] 형식으로 프롬프트 입력에 포함.

    - 마지막 user 메시지(= 현재 입력)는 history에서 제외
    - 최대 6개 메시지(3턴)만 사용
    - assistant 응답은 200자로 요약
    """
    if not messages:
        return user_query

    history = [m for m in messages if m.get("role") in (ROLE_USER, ROLE_ASSISTANT)]
    if history and history[-1].get("role") == "user":
        history = history[:-1]

    history = history[-_MAX_HISTORY_MESSAGES:]
    if not history:
        return user_query

    lines = []
    for m in history:
        role = m.get("role", "")
        content = str(m.get("content", "")).strip()
        if role == "user":
            lines.append(f"사용자: {content}")
        elif role == ROLE_ASSISTANT:
            if len(content) > _ASSISTANT_SUMMARY_LEN:
                content = content[:_ASSISTANT_SUMMARY_LEN] + "..."
            lines.append(f"챗봇: {content}")

    return f"[이전 대화]\n{chr(10).join(lines)}\n\n[현재 질문]\n{user_query}"


def _normalize_search_target(intent: str, raw: Any) -> Optional[str]:
    """통합 전처리 JSON의 search_target 정규화. search가 아니면 항상 None."""
    if intent != "search":
        return None
    if raw is None:
        return "ambiguous"
    s = str(raw).strip().lower().replace("-", "_")
    if s in SEARCH_TARGETS:
        return s
    logger.warning("[UnifiedPreprocess] 알 수 없는 search_target=%r → ambiguous", raw)
    return "ambiguous"


def _raw_policy_priority_tag_from_parsed(parsed: Dict[str, Any]) -> Any:
    """LLM JSON에서 정책 우선순위 태그 읽기(스네이크/카멜·빈값·문자열 null 허용)."""
    order = ("policy_priority_tag", "policyPriorityTag")
    for key in order:
        value = parsed.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                continue
            low = stripped.lower().replace("-", "_")
            if low in ("null", "none"):
                continue
            return low
        return value
    return None


def _normalize_detail_requested(parsed: Dict[str, Any]) -> bool:
    """LLM JSON에서 detail_requested 읽기. True/False 외 표현(문자열 'true'/'1' 등)도 허용.

    누락·파싱불가 시 False로 보수적으로 처리.
    """
    for key in ("detail_requested", "detailRequested"):
        if key in parsed:
            raw = parsed[key]
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                v = raw.strip().lower()
                if v in ("true", "1", "yes", "y"):
                    return True
                if v in ("false", "0", "no", "n", "null", ""):
                    return False
            if isinstance(raw, (int, float)):
                return bool(raw)
    return False


def _normalize_policy_priority_tag(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    value = str(raw).strip().lower().replace("-", "_")
    if not value or value in ("null", "none"):
        return None
    if value in POLICY_PRIORITY_TAGS:
        return value
    logger.warning("[UnifiedPreprocess] 알 수 없는 policy_priority_tag=%r → None", raw)
    return None


async def unified_preprocess(
    user_query: str,
    messages: Optional[List[Dict[str, Any]]] = None,
    use_rag: bool = True,
) -> Dict[str, Any]:
    """
    통합 전처리 — 단일 LLM 호출 (4가지 작업)

    반환:
        query           : 정제 + 표준어 변환된 질문
        intent          : general | comparison | guide_recommend | search
        intent_reason   : str
        reformed_query  : str
        expanded_queries: list[str]  (use_rag=False이면 [])
        keywords        : list[str]  (use_rag=False이면 [])
        search_target   : str | None (intent=search일 때만 admin_local_office | welfare_facility | ambiguous)
        policy_priority_tag: str | None (implant | low_income | elderly_benefits)
    """
    prompt_template = load_unified_preprocessing_prompt()
    if not prompt_template:
        logger.error("[UnifiedPreprocess] 프롬프트 로드 실패 — 폴백 반환")
        return _make_fallback(user_query)
    logger.debug(
        "[UnifiedPreprocess] mode=%s",
        "rewrite" if Config.QUERY_REWRITING_ENABLED else "expand",
    )

    today = date.today()
    prompt_template = (
        prompt_template
        .replace("{현재연도}", str(today.year))
        .replace("{작년연도}", str(today.year - 1))
    )

    prompt_input = _build_multiturn_input(user_query, messages or [])
    final_prompt = prompt_template.replace("{사용자 질문}", prompt_input)

    try:
        parsed, raw, used_32b = await call_classifier_with_fallback(
            classifier_name="UnifiedPreprocess",
            message=final_prompt,
            temperature=0,
            response_format={"type": "json_object"},
            extra_system_prompts=[],
        )
        if parsed is None:
            logger.warning(
                "[UnifiedPreprocess] SLM/32B 양쪽 모두 JSON 파싱 실패 → 폴백 반환 (used_32b=%s)",
                used_32b,
            )
            return _make_fallback(user_query)
        if used_32b:
            logger.info("[UnifiedPreprocess] 32B 폴백 응답으로 파싱 성공")

    except Exception as e:
        logger.error("[UnifiedPreprocess] LLM 호출 실패: %s", e, exc_info=True)
        return _make_fallback(user_query)

    query = parsed.get("query") or user_query
    reformed = parsed.get("reformed_query") or query

    intent = parsed.get("intent", "general")
    if intent not in VALID_INTENTS:
        intent = "general"

    if not use_rag:
        expanded = []
        keywords = []
    else:
        # Query Rewriting 모드: expanded_queries 를 무조건 [reformed_query] 단일 원소로 강제.
        # LLM 이 무시하고 5개를 반환해도 무력화 — 단일 검색으로 동작 보장.
        if Config.QUERY_REWRITING_ENABLED:
            expanded = [reformed] if reformed else [user_query]
        else:
            expanded = parsed.get("expanded_queries") or []
            if not expanded:
                expanded = [reformed]
        raw_reformed = reformed
        raw_expanded = list(expanded)
        reformed = sanitize_disability_text(reformed, user_query)
        expanded = [sanitize_disability_text(q, user_query) for q in expanded]
        expanded = [q for q in expanded if q]
        if not expanded:
            expanded = [reformed] if reformed else [user_query]
        # KEYWORD_BOOST_MAP 후처리: unified_preprocess LLM이 reformed_query/expanded_queries에서
        # 부스팅 키워드(예: 실직→긴급지원제도)를 떨어뜨리는 경우가 있어 검색 직전 한 번 더 복원한다.
        # 트리거 매칭은 user_query·reformed 양쪽에서 평가해 LLM이 트리거 단어 자체를 다른 표현으로
        # 바꿔도 부스팅이 발동되도록 함.
        boost_basis = f"{user_query} {reformed}"
        reformed = _apply_keyword_boost(reformed)
        expanded = [_apply_keyword_boost(q) for q in expanded]
        try:
            raw_keywords = extract_nouns(reformed)
            keywords = sanitize_disability_keywords(raw_keywords, user_query)
            # 부스팅 사업명이 명사 분해로 토큰화돼도 Mariner keyword 검색에서 정확히 잡히도록
            # 원본 사업명을 keywords 풀에도 보장 주입한다.
            for trigger, boost_terms in KEYWORD_BOOST_MAP.items():
                if trigger in boost_basis:
                    for term in boost_terms:
                        if term and term not in keywords:
                            keywords.append(term)
            if raw_reformed != reformed or raw_expanded != expanded or raw_keywords != keywords:
                logger.info(
                    "[UnifiedPreprocess] 대상집단 가드 적용 | reformed_changed=%s | expanded_changed=%s | keywords_changed=%s",
                    raw_reformed != reformed,
                    raw_expanded != expanded,
                    raw_keywords != keywords,
                )
        except Exception as e:
            logger.warning("[UnifiedPreprocess] 키워드 추출 실패: %s", e)
            keywords = []

    search_target = _normalize_search_target(intent, _raw_search_target_from_parsed(parsed))
    policy_priority_tag = _normalize_policy_priority_tag(_raw_policy_priority_tag_from_parsed(parsed))
    detail_requested = _normalize_detail_requested(parsed)
    # DB 변별 키워드로 사후 무력화 (예: "치매"가 포함되면 elderly_benefits를 None으로 강제)
    try:
        from app.chat.infra.rag.policy_priority import strip_tag_by_exclusions
        before_tag = policy_priority_tag
        policy_priority_tag = strip_tag_by_exclusions(query, policy_priority_tag)
        if before_tag and policy_priority_tag is None:
            logger.info(
                "[UnifiedPreprocess] policy_priority_tag 무력화: %s → None (DB excludes)",
                before_tag,
            )
    except Exception as e:
        logger.warning("[UnifiedPreprocess] exclude check failed: %s", e)

    result = {
        "query":            query,
        "intent":           intent,
        "intent_reason":    parsed.get("intent_reason", ""),
        "reformed_query":   reformed,
        "expanded_queries": expanded,
        "keywords":         keywords,
        "search_target":    search_target,
        "policy_priority_tag": policy_priority_tag,
        "detail_requested": detail_requested,
    }

    logger.info(
        "[UnifiedPreprocess] query=%s | intent=%s | search_target=%s | policy_priority_tag=%s | detail_requested=%s | use_rag(input)=%s",
        query[:50], intent, search_target, policy_priority_tag, detail_requested, use_rag,
    )
    return result


def _make_fallback(user_query: str) -> Dict[str, Any]:
    """LLM 호출 실패 시 안전 기본값 반환 — RAG 파이프라인이 계속 진행되도록 함."""
    return {
        **_FALLBACK,
        "query":            user_query,
        "reformed_query":   user_query,
        "expanded_queries": [user_query],
        "keywords":         [],
        "search_target":    None,
        "policy_priority_tag": None,
    }
