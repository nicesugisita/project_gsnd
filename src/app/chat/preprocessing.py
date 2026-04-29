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
from app.shared.utils.prompt_loader import load_unified_preprocessing_prompt

logger = logging.getLogger(__name__)

VALID_INTENTS = ("general", "comparison", "guide_recommend", "search")

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
    """
    prompt_template = load_unified_preprocessing_prompt()
    if not prompt_template:
        logger.error("[UnifiedPreprocess] 프롬프트 로드 실패 — 폴백 반환")
        return _make_fallback(user_query)

    today = date.today()
    prompt_template = (
        prompt_template
        .replace("{현재연도}", str(today.year))
        .replace("{작년연도}", str(today.year - 1))
    )

    prompt_input = _build_multiturn_input(user_query, messages or [])
    final_prompt = prompt_template.replace("{사용자 질문}", prompt_input)

    try:
        raw = await call_llm_api(
            message=final_prompt,
            temperature=0,
            response_format={"type": "json_object"},
            api_url=Config.LLM_API_URL,
            extra_system_prompts=[],
        )

        stripped = raw.strip()
        if stripped.startswith("```"):
            stripped = (
                stripped.removeprefix("```json")
                        .removeprefix("```")
                        .removesuffix("```")
                        .strip()
            )

        parsed = json.loads(stripped)

    except json.JSONDecodeError as e:
        logger.warning("[UnifiedPreprocess] JSON 파싱 실패: %s", e)
        return _make_fallback(user_query)
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
        try:
            raw_keywords = extract_nouns(reformed)
            keywords = sanitize_disability_keywords(raw_keywords, user_query)
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

    result = {
        "query":            query,
        "intent":           intent,
        "intent_reason":    parsed.get("intent_reason", ""),
        "reformed_query":   reformed,
        "expanded_queries": expanded,
        "keywords":         keywords,
    }

    logger.info(
        "[UnifiedPreprocess] query=%s | intent=%s | use_rag(input)=%s",
        query[:50], intent, use_rag,
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
    }
