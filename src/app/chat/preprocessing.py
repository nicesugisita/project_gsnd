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
EXCLUSION_INTENTS = frozenset({
    "NONE", "PURE_EXCLUSION", "ANCHOR_COMPARISON",
    "RESIDUAL_CATEGORY", "SUBSTITUTION",
})


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




def _normalize_exclusion_intent(raw: Any) -> str:
    """LLM JSON의 exclusion_intent 정규화. 누락/불명이면 NONE으로 폴백."""
    if raw is None:
        return "NONE"
    value = str(raw).strip().upper()
    if value in EXCLUSION_INTENTS:
        return value
    logger.warning("[UnifiedPreprocess] 알 수 없는 exclusion_intent=%r → NONE", raw)
    return "NONE"


def _normalize_str_list(raw: Any) -> List[str]:
    """배열 → strip + 빈 문자열·중복 제거된 문자열 리스트. None/타입 불일치는 []."""
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: List[str] = []
    for v in raw:
        if v is None:
            continue
        s = str(v).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _read_field(parsed: Dict[str, Any], *keys: str) -> Any:
    """스네이크/카멜 모두 허용. 첫 비-None 값 반환."""
    for k in keys:
        if k in parsed and parsed[k] is not None:
            return parsed[k]
    return None


# 생애주기 태그 화이트리스트 — lifecycle.LIFECYCLE_LABELS(6종) + 임신·출산(검색 색인 7번째 라벨).
_LIFECYCLE_TAG_WHITELIST = ("영유아", "아동", "청소년", "청년", "중장년", "노인", "임신·출산")


def _normalize_lifecycle_tags(parsed: Dict[str, Any]) -> List[str]:
    """통합 전처리 JSON의 lifecycle_tags 정규화.

    7개 라벨 화이트리스트만 통과(중복·빈값 제거, 순서 보존). 유효값 없으면 [].
    프롬프트가 미분류를 빈 배열로 주므로 별도 미분류 토큰은 무시한다.
    """
    raw = _read_field(parsed, "lifecycle_tags", "lifecycleTags")
    out: List[str] = []
    for t in _normalize_str_list(raw):
        if t in _LIFECYCLE_TAG_WHITELIST:
            out.append(t)
        else:
            logger.warning("[UnifiedPreprocess] 알 수 없는 lifecycle 태그=%r → 무시", t)
    return out


# 가구상황 태그 화이트리스트 — DB HOUSE_SITUATION 닫힌 집합. '일반가구'=특정계층 미해당.
_HOUSEHOLD_TAG_WHITELIST = ("저소득", "장애인", "한부모·조손", "다문화·탈북민", "다자녀", "보훈", "일반가구")


def _normalize_household_tags(parsed: Dict[str, Any]) -> List[str]:
    """분류기 JSON의 household_tags 정규화. 6종 화이트리스트만 통과(중복·빈값 제거, 순서 보존).

    특정계층(5종)이 하나라도 있으면 '일반가구'는 제거(특정계층 우선). 유효값 없으면 [].
    """
    raw = _read_field(parsed, "household_tags", "householdTags")
    out: List[str] = []
    for t in _normalize_str_list(raw):
        if t in _HOUSEHOLD_TAG_WHITELIST and t not in out:
            out.append(t)
        elif t not in _HOUSEHOLD_TAG_WHITELIST:
            logger.warning("[QueryTags] 알 수 없는 household 태그=%r → 무시", t)
    _specifics = [t for t in out if t != "일반가구"]
    return _specifics if _specifics else out


# 주제 category 화이트리스트 — DB 관심주제 canonical 16종.
_TOPIC_CATEGORY_WHITELIST = (
    "주거", "일자리", "보육", "교육", "신체건강", "정신건강", "보호돌봄", "생활지원",
    "안전위기", "임신출산", "문화여가", "법률", "금융", "에너지", "입양위탁", "기타",
)
# 주제 keyword 금칙어 — 일반어는 사업명 LIKE에 무의미·과다매칭이라 제외.
_TOPIC_KEYWORD_BANNED = ("지원", "지원금", "복지", "혜택", "서비스", "제도", "도움")


def _normalize_topic_category(parsed: Dict[str, Any]) -> List[str]:
    """분류기 JSON의 topic_category 정규화. canonical 16종만 통과(중복 제거, 순서 보존)."""
    raw = _read_field(parsed, "topic_category", "topicCategory")
    out: List[str] = []
    for t in _normalize_str_list(raw):
        if t in _TOPIC_CATEGORY_WHITELIST and t not in out:
            out.append(t)
        elif t not in _TOPIC_CATEGORY_WHITELIST:
            logger.warning("[QueryTags] 알 수 없는 topic_category=%r → 무시", t)
    return out


def _normalize_topic_keyword(parsed: Dict[str, Any]) -> List[str]:
    """분류기 JSON의 topic_keyword 정규화. 금칙어·1글자 제외(중복 제거, 순서 보존)."""
    raw = _read_field(parsed, "topic_keyword", "topicKeyword")
    out: List[str] = []
    for t in _normalize_str_list(raw):
        t = t.strip()
        if len(t) >= 2 and t not in _TOPIC_KEYWORD_BANNED and t not in out:
            out.append(t)
    return out


async def classify_query_tags(
    user_query: str,
    messages: Optional[List[Dict[str, Any]]] = None,
) -> tuple[Optional[List[str]], Optional[List[str]], List[str], List[str]]:
    """분리 분류기(생애주기 + 가구상황 + 주제 3축). unified_preprocess와 병렬 호출.

    Returns:
        (lifecycle_tags, household_tags, topic_category, topic_keyword)
        - lifecycle_tags: 성공 시 정규화 리스트([] 포함), LLM/파싱 실패 시 None(→ 파이프라인 룰 폴백).
        - household_tags: 성공 시 정규화 리스트(특정계층 또는 ['일반가구']), 실패 시 None(→ 룰 폴백).
        - topic_category: canonical 16종 0+개(실패 시 []). DB 추천 WHERE 주제 필터용.
        - topic_keyword: free 특정어 0+개(실패 시 []). 사업명 LIKE용(keyword 우선).
    """
    from app.shared.utils.prompt_loader import load_lifecycle_classification_prompt
    template = load_lifecycle_classification_prompt()
    parsed = None
    used_32b = False
    if template:
        final_prompt = template.replace(
            "{사용자 질문}", _build_multiturn_input(user_query, messages or [])
        )
        try:
            parsed, _raw, used_32b = await call_classifier_with_fallback(
                classifier_name="QueryTags",
                message=final_prompt,
                temperature=0,
                response_format={"type": "json_object"},
                extra_system_prompts=[],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[QueryTags] 분류 호출 실패: %s", e)
            parsed = None
    else:
        logger.warning("[QueryTags] 프롬프트 로드 실패")

    lifecycle_tags = _normalize_lifecycle_tags(parsed) if parsed is not None else None
    household_tags = _normalize_household_tags(parsed) if parsed is not None else None
    topic_category = _normalize_topic_category(parsed) if parsed is not None else []
    topic_keyword = _normalize_topic_keyword(parsed) if parsed is not None else []
    logger.info(
        "[QueryTags] lifecycle=%s | household=%s | topic_cat=%s | topic_kw=%s (used_32b=%s)",
        lifecycle_tags, household_tags, topic_category, topic_keyword, used_32b,
    )
    return lifecycle_tags, household_tags, topic_category, topic_keyword


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
    """
    # [단계 진단] 요청 시작 — 이전 요청 잔여 단계 데이터 제거 (RESPONSE_TRACE_ENABLED 시만 동작)
    try:
        from app.chat.infra.rag.stage_trace import reset as _stage_reset
        _stage_reset()
    except Exception:  # noqa: BLE001
        pass

    prompt_template = load_unified_preprocessing_prompt()
    if not prompt_template:
        logger.error("[UnifiedPreprocess] 프롬프트 로드 실패 — 폴백 반환")
        return _make_fallback(user_query)
    logger.debug("[UnifiedPreprocess] mode=rewrite")

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

    # 통합전처리 프롬프트에서 질의정제+표준어변환(작업1) 제거 — query 는 원문 user_query 그대로.
    query = user_query
    reformed = parsed.get("reformed_query") or query

    intent = parsed.get("intent", "general")
    if intent not in VALID_INTENTS:
        intent = "general"

    if not use_rag:
        expanded = []
        keywords = []
    else:
        # rewrite 모드: expanded_queries 를 [reformed_query] 단일 원소로 고정 (expansion 없음).
        expanded = [reformed] if reformed else [user_query]
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
    detail_requested = _normalize_detail_requested(parsed)

    # [작업 6] 배제 의도 분석 — rewrite 모드 프롬프트에서만 채워짐. expand 모드 폴백 시 안전 default.
    exclusion_intent = _normalize_exclusion_intent(_read_field(parsed, "exclusion_intent", "exclusionIntent"))
    raw_vector_query = _read_field(parsed, "vector_query", "vectorQuery")
    vector_query = str(raw_vector_query).strip() if raw_vector_query else (reformed or query)
    must_not_keywords = _normalize_str_list(_read_field(parsed, "must_not_keywords", "mustNotKeywords"))
    anchor_entities = _normalize_str_list(_read_field(parsed, "anchor_entities", "anchorEntities"))
    # ANCHOR_COMPARISON: must_not 은 검색을 차단하므로 강제 비움 (LLM이 잘못 채워 보내도 무력화).
    if exclusion_intent == "ANCHOR_COMPARISON":
        must_not_keywords = []
    # ANCHOR_COMPARISON 외에는 anchor_entities 의미 없음 — 강제 비움.
    if exclusion_intent != "ANCHOR_COMPARISON":
        anchor_entities = []
    result = {
        "query":            query,
        "intent":           intent,
        "intent_reason":    parsed.get("intent_reason", ""),
        "reformed_query":   reformed,
        "expanded_queries": expanded,
        "keywords":         keywords,
        "search_target":    search_target,
        "detail_requested": detail_requested,
        "exclusion_intent": exclusion_intent,
        "vector_query":     vector_query,
        "must_not_keywords": must_not_keywords,
        "anchor_entities":  anchor_entities,
    }

    logger.info(
        "[UnifiedPreprocess] query=%s | intent=%s | search_target=%s | detail_requested=%s | exclusion_intent=%s | mustNot=%s | anchor=%s | use_rag(input)=%s",
        query[:50], intent, search_target, detail_requested,
        exclusion_intent, must_not_keywords, anchor_entities, use_rag,
    )

    # [단계 진단] 전처리 산출(재구성/의도/리라이팅/키워드/정책태그/벡터쿼리) 기록
    try:
        from app.chat.infra.rag.stage_trace import record_preprocess as _stage_record_preprocess
        _stage_record_preprocess(result)
    except Exception:  # noqa: BLE001
        pass

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
        "exclusion_intent": "NONE",
        "vector_query":     user_query,
        "must_not_keywords": [],
        "anchor_entities":  [],
    }
