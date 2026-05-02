"""
SuggestQuestionsService — 추천 질문 생성 (Phase 2: 생성자 주입).

conv_id로 불러온 히스토리의 마지막 user/assistant 턴과 referenced_documents 발췌를
시스템 프롬프트에 넣고, 근거 내에서만 답 가능한 후속 질문을 생성한다.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from app.core.constants import ROLE_ASSISTANT, ROLE_USER
from app.core.protocols import LLMClientProtocol, SuggestQuestionsServiceProtocol
from app.shared.utils.prompt_loader import load_suggest_questions_prompt

logger = logging.getLogger(__name__)

_PLACEHOLDER_USER = "<<__SUGGEST_LAST_USER_QUESTION__>>"
_PLACEHOLDER_ANSWER = "<<__SUGGEST_LAST_ASSISTANT_ANSWER__>>"
_PLACEHOLDER_REFS = "<<__SUGGEST_REFERENCE_EXCERPTS__>>"

_RESULT_KEYS = (
    "questions",
    "suggested_questions",
    "recommended_questions",
    "추천_질문",
    "추천질문",
    "후속_질문",
)


def _refs_from_assistant_message(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    meta = msg.get("metadata")
    if isinstance(meta, dict):
        rd = meta.get("referenced_documents")
        if isinstance(rd, list):
            return [x for x in rd if isinstance(x, dict)]
    rd = msg.get("referenced_documents")
    if isinstance(rd, list):
        return [x for x in rd if isinstance(x, dict)]
    return []


def intent_from_assistant_message(msg: Dict[str, Any]) -> Optional[str]:
    """히스토리 assistant 메시지에 저장된 의도(intent). preprocess 우선."""
    pp = msg.get("preprocess")
    if isinstance(pp, dict):
        inn = pp.get("intent")
        if isinstance(inn, str) and inn.strip():
            return inn.strip().lower()
    md = msg.get("metadata")
    if isinstance(md, dict):
        inn = md.get("intent")
        if isinstance(inn, str) and inn.strip():
            return inn.strip().lower()
    return None


def last_assistant_intent_from_messages(messages: List[Dict[str, Any]]) -> Optional[str]:
    """내용이 있는 마지막 assistant 턴의 intent (동일 규칙 as extract_last_turn_context)."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") != ROLE_ASSISTANT or not str(m.get("content") or "").strip():
            continue
        return intent_from_assistant_message(m)
    return None


def extract_last_turn_context(messages: List[Dict[str, Any]]) -> tuple[str, str, List[Dict[str, Any]]]:
    """마지막 비어 있지 않은 assistant와 그 직전 user, 해당 assistant의 참조 문서."""
    if not messages:
        return "", "", []
    last_asst_idx: Optional[int] = None
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == ROLE_ASSISTANT and str(m.get("content") or "").strip():
            last_asst_idx = i
            break
    if last_asst_idx is None:
        return "", "", []
    asst_msg = messages[last_asst_idx]
    answer = str(asst_msg.get("content") or "").strip()
    refs = _refs_from_assistant_message(asst_msg)
    user_q = ""
    for j in range(last_asst_idx - 1, -1, -1):
        if messages[j].get("role") == ROLE_USER:
            user_q = str(messages[j].get("content") or "").strip()
            break
    return user_q, answer, refs


def format_reference_excerpts_for_prompt(
    refs: List[Dict[str, Any]],
    *,
    max_docs: int = 12,
    max_snippet_chars: int = 1600,
) -> str:
    """참고 문서 블록 (이름·chunk_id·발췌). 발췌 없으면 문서명만 표시."""
    if not refs:
        return "(참고 문서 없음)"
    lines: List[str] = []
    for i, d in enumerate(refs[:max_docs], 1):
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or "").strip()
        cid = str(d.get("chunk_id") or d.get("id") or "").strip()
        snip = str(d.get("snippet") or "").strip()
        if len(snip) > max_snippet_chars:
            snip = snip[:max_snippet_chars] + "…"
        head = f"[참고 {i}]"
        if name:
            head += f" {name}"
        if cid:
            head += f" (chunk_id={cid})"
        lines.append(head)
        if snip:
            lines.append(snip)
        else:
            lines.append("(저장된 본문 발췌 없음 — 답변 본문·문서명 범위에서만 질문 생성)")
        lines.append("")
    return "\n".join(lines).strip() or "(참고 문서 없음)"


def build_suggest_questions_system_prompt(
    user_query: str,
    assistant_response: str,
    reference_excerpts: str,
) -> str:
    template = load_suggest_questions_prompt()
    if not template:
        return ""
    prompt = template
    prompt = prompt.replace(_PLACEHOLDER_USER, user_query or "(없음)")
    prompt = prompt.replace(_PLACEHOLDER_ANSWER, assistant_response or "(없음)")
    prompt = prompt.replace(_PLACEHOLDER_REFS, reference_excerpts or "(참고 문서 없음)")
    return prompt


async def run_suggest_questions_core(
    *,
    max_questions: int,
    messages: Optional[List[Dict[str, Any]]] = None,
    user_query: str = "",
    assistant_response: str = "",
    http_client: Any = None,
) -> List[str]:
    """히스토리 또는 직접 전달된 user/answer로 추천 질문 LLM 호출."""
    from app.chat.infra.llm.core import call_llm_api

    u, a, refs = ("", "", [])
    if messages:
        if last_assistant_intent_from_messages(messages) == "search":
            logger.info("[SuggestQuestions] intent=search → 추천 질문 생성 생략")
            return []
        u, a, refs = extract_last_turn_context(messages)
        if not a:
            logger.warning("[SuggestQuestions] 히스토리에서 assistant 답변을 찾지 못함")
            return []
        user_query, assistant_response = u or user_query, a

    if not user_query.strip() or not assistant_response.strip():
        logger.warning("[SuggestQuestions] user_query 또는 assistant_response 비어 있음")
        return []

    ref_block = format_reference_excerpts_for_prompt(refs)
    prompt = build_suggest_questions_system_prompt(user_query, assistant_response, ref_block)
    if not prompt:
        logger.warning("[SuggestQuestions] 프롬프트 로드 실패")
        return []

    call_messages = [
        {"role": ROLE_USER, "content": user_query},
        {"role": ROLE_ASSISTANT, "content": assistant_response},
        {"role": ROLE_USER, "content": "위 근거 범위에서만 관련 후속 질문을 JSON으로 제시해 주세요."},
    ]

    kw: Dict[str, Any] = dict(
        temperature=0,
        messages=call_messages,
        extra_system_prompts=[prompt],
        response_format={"type": "json_object"},
    )
    if http_client is not None:
        kw["http_client"] = http_client

    try:
        raw = await call_llm_api(**kw)
    except Exception:
        logger.exception("[SuggestQuestions] LLM 호출 실패")
        return []

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[SuggestQuestions] JSON 파싱 실패: %s", str(raw)[:200])
        return []

    if isinstance(result, list):
        questions = result
    elif isinstance(result, dict):
        questions = []
        for key in _RESULT_KEYS:
            if key in result and isinstance(result[key], list):
                questions = result[key]
                break
        if not questions:
            questions = next((v for v in result.values() if isinstance(v, list)), [])
    else:
        questions = []

    questions = [q for q in questions if isinstance(q, str) and q.strip()]
    questions = questions[:max_questions]
    logger.info("[SuggestQuestions] 생성된 추천 질문 %d개", len(questions))
    return questions


class SuggestQuestionsService:
    """
    추천 질문 생성 서비스.

    Attributes:
        _client: LLMClientProtocol을 구현한 httpx.AsyncClient (또는 Mock)
    """

    def __init__(self, client: LLMClientProtocol) -> None:
        self._client = client

    async def generate(
        self,
        max_questions: int = 5,
        *,
        messages: Optional[List[Dict[str, Any]]] = None,
        user_query: str = "",
        assistant_response: str = "",
    ) -> list[str]:
        return await run_suggest_questions_core(
            max_questions=max_questions,
            messages=messages,
            user_query=user_query,
            assistant_response=assistant_response,
            http_client=self._client,
        )


assert isinstance(SuggestQuestionsService(None), SuggestQuestionsServiceProtocol), (
    "SuggestQuestionsService must satisfy SuggestQuestionsServiceProtocol"
)
