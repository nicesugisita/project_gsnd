"""ContextualQueryRewriter — 사용자 질문을 RAG 검색용으로 3-way 재작성.

핸드오프 문서: contextual_query_rewriter_handoff.md (Java/Spring 원본의 Python 이식).

사용자 질문 1건 → 1회 32B LLM 호출 → 다음 3개 산출:
- vector_query : 벡터 임베딩 검색용 (제외 entity 단어 제거, ANCHOR 케이스는 유지)
- llm_query    : LLM 답변용 자연어 (의도 명시)
- must_not_keywords : 키워드 검색 must_not 절 후보

5가지 ExclusionIntent 중 1개로 분류:
NONE / PURE_EXCLUSION / ANCHOR_COMPARISON / RESIDUAL_CATEGORY / SUBSTITUTION

LLM 장애·JSON 파싱 실패 시 원본 쿼리 그대로 반환하는 default 결과로 폴백.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
import json
import logging
import re
from typing import Any, Callable, List, Optional

from app.chat.infra.llm.core import call_llm_api
from app.core.exceptions import LLMServiceError
from app.shared.utils.prompt_loader import (
    load_contextual_query_rewriter_multi_turn_prompt,
    load_contextual_query_rewriter_output_spec,
    load_contextual_query_rewriter_single_turn_prompt,
)

logger = logging.getLogger(__name__)


class ExclusionIntent(str, Enum):
    NONE = "NONE"
    PURE_EXCLUSION = "PURE_EXCLUSION"
    ANCHOR_COMPARISON = "ANCHOR_COMPARISON"
    RESIDUAL_CATEGORY = "RESIDUAL_CATEGORY"
    SUBSTITUTION = "SUBSTITUTION"


@dataclass
class QueryRewriteResult:
    original_query: str
    vector_query: str
    llm_query: str
    must_keywords: List[str] = field(default_factory=list)
    must_not_keywords: List[str] = field(default_factory=list)
    anchor_entities: List[str] = field(default_factory=list)
    exclusion_intent: ExclusionIntent = ExclusionIntent.NONE
    rewritten: bool = False
    has_exclusion: bool = False


_CODE_FENCE_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_RAW_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _format_history(messages: Optional[List[dict]]) -> str:
    """messages 리스트를 'role: content' 멀티라인 문자열로 직렬화."""
    if not messages:
        return ""
    lines: List[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if not role or not content:
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _build_multi_turn_user_prompt(history: str, question: str, today: date) -> str:
    year = today.year
    weekday = today.strftime("%A")
    return (
        f"CURRENT DATE CONTEXT (use this to resolve relative time expressions):\n"
        f"- Today: {today.isoformat()} ({weekday})\n"
        f"- 작년 = {year - 1}, 재작년 = {year - 2}, 올해 = {year}, 내년 = {year + 1}\n"
        f"- \"last year\" = {year - 1}, \"this year\" = {year}, \"next year\" = {year + 1}\n\n"
        f"Conversation history:\n{history}\n\n"
        f"Latest question:\n{question}\n\n"
        f"Rewritten self-contained question:"
    )


def _build_single_turn_user_prompt(question: str, today: date) -> str:
    year = today.year
    weekday = today.strftime("%A")
    return (
        f"CURRENT DATE CONTEXT (use this to resolve relative time expressions):\n"
        f"- Today: {today.isoformat()} ({weekday})\n"
        f"- 작년 = {year - 1}, 재작년 = {year - 2}, 올해 = {year}, 내년 = {year + 1}\n\n"
        f"Question:\n{question}\n\n"
        f"Rewritten question:"
    )


def _extract_json(response: str) -> str:
    """코드펜스(```json ... ```) 우선, 없으면 첫 raw JSON object 매칭, 둘 다 없으면 원문."""
    if not response:
        return ""
    m = _CODE_FENCE_JSON_RE.search(response)
    if m:
        return m.group(1)
    m = _RAW_JSON_RE.search(response)
    if m:
        return m.group(0)
    return response


def _parse_intent(value: Any) -> ExclusionIntent:
    if value is None:
        return ExclusionIntent.NONE
    raw = str(value).strip().upper()
    try:
        return ExclusionIntent(raw)
    except ValueError:
        logger.warning(
            "[ContextualQueryRewriter] Unknown exclusionIntent '%s' → NONE 폴백", raw
        )
        return ExclusionIntent.NONE


def _as_str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for v in value:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out.append(s)
    return out


def _strip_quotes(text: str) -> str:
    text = (text or "").strip()
    if len(text) >= 2 and (
        (text[0] == '"' and text[-1] == '"') or (text[0] == "'" and text[-1] == "'")
    ):
        return text[1:-1].strip()
    return text


def _default_result(question: str) -> QueryRewriteResult:
    q = question or ""
    return QueryRewriteResult(
        original_query=q,
        vector_query=q,
        llm_query=q,
        exclusion_intent=ExclusionIntent.NONE,
        rewritten=False,
        has_exclusion=False,
    )


def _parse_structured(response: str, fallback: str) -> Optional[QueryRewriteResult]:
    """LLM 응답 → QueryRewriteResult. 실패 시 None (호출자가 default로 폴백)."""
    try:
        parsed = json.loads(_extract_json(response))
    except Exception as e:  # noqa: BLE001 — 어떤 파싱 실패든 default 폴백
        logger.warning(
            "[ContextualQueryRewriter] JSON 파싱 실패: %s | response prefix: %s",
            e, (response or "")[:200],
        )
        return None

    if not isinstance(parsed, dict):
        logger.warning("[ContextualQueryRewriter] JSON 최상위가 객체가 아님 → 폴백")
        return None

    intent = _parse_intent(parsed.get("exclusionIntent"))
    must_not = _as_str_list(parsed.get("mustNotKeywords"))
    # ANCHOR_COMPARISON: LLM이 잘못 채워 보내도 강제로 비움 (검색 차단 안 함)
    if intent == ExclusionIntent.ANCHOR_COMPARISON:
        must_not = []

    vector_query = _strip_quotes(str(parsed.get("vectorQuery") or fallback))
    llm_query = _strip_quotes(str(parsed.get("llmQuery") or fallback))

    has_exclusion_raw = parsed.get("hasExclusion")
    if isinstance(has_exclusion_raw, bool):
        has_exclusion = has_exclusion_raw
    else:
        has_exclusion = intent != ExclusionIntent.NONE

    return QueryRewriteResult(
        original_query=fallback,
        vector_query=vector_query,
        llm_query=llm_query,
        must_keywords=_as_str_list(parsed.get("mustKeywords")),
        must_not_keywords=must_not,
        anchor_entities=_as_str_list(parsed.get("anchorEntities")),
        exclusion_intent=intent,
        has_exclusion=has_exclusion,
    )


async def rewrite_query(
    question: str,
    messages: Optional[List[dict]] = None,
    *,
    today_provider: Callable[[], date] = date.today,
    temperature: float = 0,
    max_tokens: int = 1024,
) -> QueryRewriteResult:
    """질문을 3-way 재작성 (32B LLM 호출).

    Args:
        question: 사용자 최신 질문.
        messages: 멀티턴 대화 이력 ([{"role": "user"/"assistant", "content": ...}, ...]).
            None 또는 빈 리스트면 single-turn 프롬프트 사용.
        today_provider: 현재 날짜 주입 함수. 테스트에서 고정값 주입용.
        temperature: 분류 일관성을 위해 0 고정 권장.
        max_tokens: OUTPUT_SPEC + JSON 응답에 충분한 토큰.

    Returns:
        QueryRewriteResult. LLM 장애·파싱 실패 시 원본 그대로의 default 결과.
    """
    if not question or not question.strip():
        return _default_result(question or "")

    history_text = _format_history(messages)
    has_history = bool(history_text.strip())

    if has_history:
        system_prompt = (
            load_contextual_query_rewriter_multi_turn_prompt()
            + "\n\n"
            + load_contextual_query_rewriter_output_spec()
        )
        user_prompt = _build_multi_turn_user_prompt(
            history_text, question, today_provider()
        )
    else:
        system_prompt = (
            load_contextual_query_rewriter_single_turn_prompt()
            + "\n\n"
            + load_contextual_query_rewriter_output_spec()
        )
        user_prompt = _build_single_turn_user_prompt(question, today_provider())

    try:
        response = await call_llm_api(
            message=user_prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
    except LLMServiceError as e:
        logger.warning("[ContextualQueryRewriter] LLM 호출 실패: %s → 원본 폴백", e)
        return _default_result(question)
    except Exception as e:  # noqa: BLE001 — 예기치 못한 예외도 원본 폴백
        logger.warning(
            "[ContextualQueryRewriter] 예상치 못한 예외: %s → 원본 폴백", e
        )
        return _default_result(question)

    if not isinstance(response, str) or not response.strip():
        return _default_result(question)

    parsed = _parse_structured(response, question)
    if parsed is None:
        return _default_result(question)

    parsed.original_query = question
    parsed.rewritten = (
        parsed.vector_query.strip() != question.strip()
        or parsed.llm_query.strip() != question.strip()
    )

    logger.info(
        "[ContextualQueryRewriter] [%s] '%s' → vec='%s' llm='%s' mustNot=%s anchor=%s intent=%s",
        "multi-turn" if has_history else "single-turn",
        question, parsed.vector_query, parsed.llm_query,
        parsed.must_not_keywords, parsed.anchor_entities,
        parsed.exclusion_intent.value,
    )
    return parsed
