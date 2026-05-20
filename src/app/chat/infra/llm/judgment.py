"""판단 로직 및 유틸리티 (clarification, RAG/NO-RAG, 추천 질문, 요약, 의도 분류)"""

import asyncio
import json
import logging
from typing import Any, Dict, List

from app.core.config import Config
from app.core.constants import ROLE_USER, ROLE_ASSISTANT
from app.core.exceptions import LLMServiceError
from app.shared.utils.prompt_loader import (
    load_ask_judgment_prompt,
    load_re_ask_prompt,
    load_document_summary_prompt,
    load_pre_check_prompt,
)
from app.chat.infra.deepserver.client import (
    deepserver_rag_norag_judgment,
    deepserver_intent_classification,
)

from .core import call_llm_api
from .classifier_fallback import call_classifier_with_fallback

logger = logging.getLogger(__name__)


async def ask_judgment(user_query: str, messages: list = None) -> Dict[str, bool]:
    """
    사용자 질문이 추가 설명(clarification)이 필요한지 판단

    Returns:
        {"need_clarify": bool, "clarify_reason": str}
    """
    try:
        prompt = load_ask_judgment_prompt()
        if not prompt:
            logger.warning("[Ask Judgment] 프롬프트 로드 실패")
            return {"need_clarify": False}

        system_prompt = prompt.replace("{질문}", user_query)

        history = []
        if messages:
            max_messages = Config.MULTITURN_MAX_TURNS * 2
            recent = list(messages[-max_messages:])
            while recent and recent[-1].get("role") == ROLE_USER:
                recent.pop()
            for m in recent:
                role = m.get("role", "")
                content = m.get("content", "")
                if role in (ROLE_USER, ROLE_ASSISTANT) and content:
                    history.append({"role": role, "content": content})

        llm_messages = history + [{"role": ROLE_USER, "content": user_query}]

        response = await call_llm_api(
            temperature=0,
            messages=llm_messages,
            extra_system_prompts=[system_prompt],
            response_format={"type": "json_object"},
        )

        if isinstance(response, str) and response.startswith("```"):
            response = response.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        result = json.loads(response)
        need_clarify = result.get("need_clarify", False)
        clarify_reason = result.get("reason", "")
        logger.info("[Ask Judgment] 질문: %s | need_clarify=%s | reason=%s", user_query[:50], need_clarify, clarify_reason)
        return {"need_clarify": need_clarify, "clarify_reason": clarify_reason}

    except json.JSONDecodeError as e:
        logger.warning(f"[Ask Judgment] JSON 파싱 실패: {e}")
        return {"need_clarify": False}
    except Exception as e:
        logger.error(f"[Ask Judgment] 예상치 못한 오류: {e}", exc_info=True)
        return {"need_clarify": False}


async def mandatory_condition_check(user_query: str, messages: list = None) -> Dict[str, bool]:
    """답변 생성을 위해 추가 조건 정보가 필요한지 판단"""
    return {"need_collect": False}


async def re_ask(reason_from_previous_step: str, previous_messages: str, user_query: str) -> str:
    """
    사용자 질문에 대해 추가 정보를 묻는 질문 생성

    Returns:
        추가 정보를 묻는 질문 (생성 실패 시 기본 메시지)
    """
    default_message = "질문이 불명확합니다. 더 자세히 설명해주실 수 있을까요?"

    try:
        prompt = load_re_ask_prompt()
        if not prompt:
            logger.warning("[Re-Ask] 프롬프트 로드 실패")
            return default_message

        input_data = {
            "reason_from_previous_step": reason_from_previous_step,
            "previous_messages": previous_messages,
            "user_query": user_query
        }
        call_messages = [{"role": "user", "content": input_data}]
        logger.info(f"[re_ask] previous_messages: {previous_messages}")
        logger.info(f"[re_ask] call_messages: {call_messages}")

        response = await call_llm_api(
            temperature=0,
            messages=call_messages,
            extra_system_prompts=[prompt]
        )

        if isinstance(response, str) and response.strip():
            logger.info("[Re-Ask] 되묻기 질문 생성: %s", response)
            return response.strip()

        logger.warning("[Re-Ask] 빈 응답 수신")
        return default_message

    except LLMServiceError as e:
        logger.warning(f"[Re-Ask] LLM 오류: {e}")
        return default_message
    except Exception as e:
        logger.error(f"[Re-Ask] 예상치 못한 오류: {e}", exc_info=True)
        return default_message


_PRE_CHECK_MAX_HISTORY = 6
_PRE_CHECK_ASSISTANT_SUMMARY_LEN = 200


def _build_pre_check_input(user_query: str, messages: list) -> str:
    """이전 대화 이력을 [이전 대화] 형식으로 포함한 pre_check 입력 구성."""
    if not messages:
        return user_query

    history = [m for m in messages if m.get("role") in (ROLE_USER, ROLE_ASSISTANT)]
    if history and history[-1].get("role") == "user":
        history = history[:-1]
    history = history[-_PRE_CHECK_MAX_HISTORY:]
    if not history:
        return user_query

    lines = []
    for m in history:
        role = m.get("role", "")
        content = str(m.get("content", "")).strip()
        if role == "user":
            lines.append(f"사용자: {content}")
        elif role == ROLE_ASSISTANT:
            if len(content) > _PRE_CHECK_ASSISTANT_SUMMARY_LEN:
                content = content[:_PRE_CHECK_ASSISTANT_SUMMARY_LEN] + "..."
            lines.append(f"챗봇: {content}")

    return f"[이전 대화]\n{chr(10).join(lines)}\n\n[현재 질문]\n{user_query}"


async def pre_check(user_query: str, messages: list = None) -> Dict[str, Any]:
    """
    선행 판단: use_rag + 되묻기 필요 여부를 단일 LLM 호출로 처리.

    Returns:
        {
          "use_rag": bool,
          "clarification_question": str  # 되묻기 질문 또는 ""
        }
    """
    _fallback = {"use_rag": True, "clarification_question": ""}

    try:
        from datetime import date
        prompt_template = load_pre_check_prompt()
        if not prompt_template:
            logger.warning("[PreCheck] 프롬프트 로드 실패 — 폴백 반환")
            return _fallback

        today = date.today()
        prompt_template = (
            prompt_template
            .replace("{현재연도}", str(today.year))
            .replace("{작년연도}", str(today.year - 1))
        )

        prompt_input = _build_pre_check_input(user_query, messages or [])
        final_prompt = prompt_template.replace("{사용자 질문}", prompt_input)

        parsed, raw, used_32b = await call_classifier_with_fallback(
            classifier_name="PreCheck",
            message=final_prompt,
            temperature=0,
            response_format={"type": "json_object"},
            extra_system_prompts=[],
        )
        if parsed is None:
            logger.warning(
                "[PreCheck] SLM/32B 모두 JSON 파싱 실패 → 폴백 반환 (used_32b=%s)",
                used_32b,
            )
            return _fallback
        if used_32b:
            logger.info("[PreCheck] 32B 폴백 응답으로 파싱 성공")
        use_rag = bool(parsed.get("use_rag", True))
        clarification_question = str(parsed.get("clarification_question", "")).strip()

        if not use_rag:
            clarification_question = ""

        logger.info(
            "[PreCheck] use_rag=%s | clarification_question=%s",
            use_rag,
            clarification_question[:60] if clarification_question else "(없음)",
        )
        return {"use_rag": use_rag, "clarification_question": clarification_question}

    except json.JSONDecodeError as e:
        logger.warning("[PreCheck] JSON 파싱 실패: %s", e)
        return _fallback
    except Exception as e:
        logger.error("[PreCheck] 오류: %s", e, exc_info=True)
        return _fallback


async def rag_norag_judgment(user_query: str, messages: list = None) -> Dict[str, bool]:
    """
    사용자 질문이 RAG 대상인지 여부 판단

    Returns:
        {"use_rag": bool}
    """
    try:
        response = await deepserver_rag_norag_judgment(user_query, messages=messages)
        result = json.loads(response)
        use_rag = result.get("use_rag", True)
        logger.info("[RAG/NO-RAG Judgment] 질문: %s | use_rag=%s", user_query[:50], use_rag)
        return {"use_rag": use_rag}

    except json.JSONDecodeError as e:
        logger.warning(f"[RAG/NO-RAG Judgment] JSON 파싱 실패: {e}")
        return {"use_rag": True}
    except Exception as e:
        logger.error(f"[RAG/NO-RAG Judgment] 예상치 못한 오류: {e}", exc_info=True)
        return {"use_rag": True}


async def generate_suggested_questions(
    user_query: str,
    assistant_response: str,
    max_questions: int = None
) -> list:
    """
    사용자 질문과 어시스턴트 답변을 기반으로 추천 질문 생성

    Returns:
        추천 질문 리스트
    """
    from app.chat.suggest_questions_service import run_suggest_questions_core

    try:
        if max_questions is None:
            max_questions = Config.MAX_SUGGESTED_QUESTIONS
        return await run_suggest_questions_core(
            max_questions=max_questions,
            messages=None,
            user_query=user_query,
            assistant_response=assistant_response,
            http_client=None,
        )
    except LLMServiceError as e:
        logger.warning(f"[Suggest Questions] LLM 오류: {e}")
        return []
    except Exception as e:
        logger.error(f"[Suggest Questions] 예상치 못한 오류: {e}", exc_info=True)
        return []


def _split_document_text_for_summary(
    text: str,
    max_chars_per_chunk: int = 10000,
    overlap_chars: int = 200
) -> List[str]:
    """긴 문서를 요약용 청크로 분할."""
    normalized_text = (text or "").strip()
    if not normalized_text:
        return []

    if len(normalized_text) <= max_chars_per_chunk:
        return [normalized_text]

    chunks: List[str] = []
    start = 0
    text_len = len(normalized_text)

    while start < text_len:
        end = min(start + max_chars_per_chunk, text_len)

        if end < text_len:
            search_start = start + int(max_chars_per_chunk * 0.6)
            paragraph_break = normalized_text.rfind("\n\n", search_start, end)
            line_break = normalized_text.rfind("\n", search_start, end)
            word_break = normalized_text.rfind(" ", search_start, end)
            best_break = max(paragraph_break, line_break, word_break)
            if best_break > start:
                end = best_break

        chunk = normalized_text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= text_len:
            break

        start = max(end - overlap_chars, start + 1)

    return chunks


async def summarize_document_text(
    document_text: str,
    max_chars_per_chunk: int = 10000,
    max_concurrent_chunks: int = 10,
) -> str:
    """
    문서 원문을 요약. 긴 문서는 청크별 요약 후 최종 통합(Map-Reduce) 요약을 수행합니다.
    """
    text = (document_text or "").strip()
    if not text:
        return ""

    try:
        summary_prompt = load_document_summary_prompt()
        if not summary_prompt:
            summary_prompt = (
                "당신은 문서 요약 전문가입니다. 원문 의미를 훼손하지 말고 핵심만 한국어로 요약하세요. "
                "중요 수치, 대상, 기간, 조건을 우선 보존하세요."
            )

        chunks = _split_document_text_for_summary(
            text=text,
            max_chars_per_chunk=max_chars_per_chunk
        )

        if not chunks:
            return ""

        logger.info("[Document Summary] 총 청크 수: %d", len(chunks))

        if len(chunks) == 1:
            response = await call_llm_api(
                temperature=0,
                messages=[{"role": ROLE_USER, "content": f"문서 원문:\n\n{chunks[0]}"}],
                extra_system_prompts=[summary_prompt],
                max_tokens=1200
            )
            return response.strip() if isinstance(response, str) else ""

        total_chunks = len(chunks)
        semaphore = asyncio.Semaphore(max_concurrent_chunks)

        async def summarize_chunk(index: int, chunk: str) -> tuple:
            async with semaphore:
                chunk_user_content = (
                    f"전체 {total_chunks}개 중 {index}번째 청크입니다. "
                    "아래 청크의 핵심만 5~8문장으로 요약하세요.\n\n"
                    f"{chunk}"
                )
                chunk_response = await call_llm_api(
                    temperature=0,
                    messages=[{"role": ROLE_USER, "content": chunk_user_content}],
                    extra_system_prompts=[summary_prompt],
                    max_tokens=700
                )
                result = chunk_response.strip() if isinstance(chunk_response, str) else ""
                logger.debug("[Document Summary] 청크 %d/%d 완료", index, total_chunks)
                return index, result

        tasks = [summarize_chunk(i, chunk) for i, chunk in enumerate(chunks, start=1)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        chunk_summaries: List[str] = [""] * total_chunks
        for item in results:
            if isinstance(item, Exception):
                logger.warning("[Document Summary] 청크 요약 실패: %s", item)
                continue
            index, summary = item
            if summary:
                chunk_summaries[index - 1] = summary

        chunk_summaries = [s for s in chunk_summaries if s]
        if not chunk_summaries:
            return ""

        merged_summary_input = "\n\n".join(
            [f"[청크 {i}] {summary}" for i, summary in enumerate(chunk_summaries, start=1)]
        )

        final_response = await call_llm_api(
            temperature=0,
            messages=[{
                "role": ROLE_USER,
                "content": (
                    "다음은 문서 청크별 중간 요약입니다. 이를 바탕으로 최종 통합 요약을 작성하세요.\n"
                    "출력 형식: 1) 전체 요지 2) 주요 항목(불릿) 3) 주의/조건(있을 경우)\n\n"
                    f"{merged_summary_input}"
                )
            }],
            extra_system_prompts=[summary_prompt],
            max_tokens=1500
        )

        return final_response.strip() if isinstance(final_response, str) else "\n\n".join(chunk_summaries)

    except Exception as e:
        logger.error(f"[Document Summary] 요약 실패: {e}", exc_info=True)
        return ""


async def intent_classification(user_query: str, messages: list = None) -> Dict[str, Any]:
    """
    사용자 질문의 의도 분류 (general / comparison / guide_recommend / search)

    Returns:
        {"intent": str, "reason": str}
    """
    try:
        response = await deepserver_intent_classification(user_query)
        result = json.loads(response)
        intent = result.get("intent", "general")
        reason = result.get("reason", "")

        valid_intents = ["general", "comparison", "guide_recommend", "search"]
        if intent not in valid_intents:
            logger.warning(f"[Intent Classification] 유효하지 않은 intent: {intent}, general로 설정")
            intent = "general"

        logger.info(f"[Intent Classification] 질문: {user_query[:50]} | intent={intent} | reason={reason}")
        return {"intent": intent, "reason": reason}

    except json.JSONDecodeError as e:
        logger.warning(f"[Intent Classification] JSON 파싱 실패: {e}, general로 기본값 반환")
        return {"intent": "general", "reason": "JSON 파싱 실패"}
    except Exception as e:
        logger.error(f"[Intent Classification] 예상치 못한 오류: {e}", exc_info=True)
        return {"intent": "general", "reason": "예상치 못한 오류"}
