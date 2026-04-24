"""최종 응답 생성 (intent별 프롬프트 분기)"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.constants import ROLE_USER, ROLE_ASSISTANT, LLM_MAX_TOTAL_DOC_CHARS, LLM_MAX_DOC_CHUNK_CHARS
from utils.prompt_loader import (
    load_classification_general_prompt,
    load_classification_comparison_prompt,
    load_classification_recommended_prompt,
)

from .document import _format_document_for_prompt, _format_facility_for_prompt
from .token import truncate_messages_by_token_limit

logger = logging.getLogger(__name__)


async def _generate_final_response(
    message: str,
    top3_docs: List[Dict[str, Any]],
    temperature: float,
    max_tokens: int,
    stream: bool,
    frequency_penalty: float,
    repetition_penalty: float,
    top_p: float,
    top_k: int,
    seed: int,
    tools: list,
    intent: str = "general",
    welfare_docs: Optional[List[Dict[str, Any]]] = None,
    lifecycle: str = "",
    messages: list = None,
) -> Any:
    """
    최종 응답 생성 (intent에 따라 다른 프롬프트 사용)

    intent:
    - "general": classification_general_prompt.txt 사용
    - "comparison": classification_comparison_prompt.txt 사용
    - "guide_recommend": classification_recommended_prompt.txt 사용
    """
    from services.llm_service import call_llm_api

    try:
        max_total_doc_chars = LLM_MAX_TOTAL_DOC_CHARS
        max_doc_chunk_chars = LLM_MAX_DOC_CHUNK_CHARS
        doc_content = ""
        for i, doc in enumerate(top3_docs, 1):
            candidate = _format_document_for_prompt(doc, i, max_doc_chunk_chars)
            if len(doc_content) + len(candidate) > max_total_doc_chars:
                remain = max_total_doc_chars - len(doc_content)
                if remain > 0:
                    doc_content += candidate[:remain]
                break
            doc_content += candidate

        if not doc_content:
            doc_content = "제공된 참고 문서가 없습니다."

        if intent == "comparison":
            final_prompt = load_classification_comparison_prompt()
            logger.info("[Final Response] Comparison 프롬프트 사용")
        elif intent == "guide_recommend":
            final_prompt = load_classification_recommended_prompt()
            logger.info("[Final Response] Guide_Recommend 프롬프트 사용")
        else:
            final_prompt = load_classification_general_prompt()
            logger.info("[Final Response] General 프롬프트 사용")

        if not final_prompt:
            logger.warning(f"[Final Response] {intent} 프롬프트 로드 실패 - 기본 LLM 사용")
            return await call_llm_api(
                temperature=temperature,
                max_tokens=max_tokens,
                messages=[{"role": ROLE_USER, "content": message}],
                frequency_penalty=frequency_penalty,
                repetition_penalty=repetition_penalty,
                top_p=top_p,
                top_k=top_k,
                seed=seed,
                tools=tools
            )

        system_prompt = final_prompt
        user_region = "정보 없음"
        user_birth_year = "정보 없음"
        if intent == "guide_recommend":
            def _extract_region_birth_year(text: str) -> tuple:
                age_match = re.search(r'(\d{1,2})\s*(?:세|살)', text)
                if age_match:
                    birth_year = str(datetime.now().year - int(age_match.group(1)))
                else:
                    year_match = re.search(r"(19\d{2}|20\d{2})", text)
                    birth_year = year_match.group(1) if year_match else "정보 없음"
                clean_text = re.sub(r"\d+\s*(?:년생|년|세|살)", "", text)
                region_match = re.search(r"([가-힣]{2,}(?:시|군|구)?)", clean_text)
                region = region_match.group(1) if region_match else "정보 없음"
                return region, birth_year

            user_region, user_birth_year = _extract_region_birth_year(message)

        facility_content = ""
        if welfare_docs:
            for i, wdoc in enumerate(welfare_docs, 1):
                facility_content += _format_facility_for_prompt(wdoc, i)

        if intent == "guide_recommend":
            user_life_stage = lifecycle if lifecycle else "정보 없음"
            user_message = f"""사용자 질문: {message}
        user_region: {user_region}
        user_birth_year: {user_birth_year}
        user_life_stage: {user_life_stage}
        retrieved_documents:
        {doc_content}"""
            if facility_content:
                user_message += f"\n        retrieved_facilities:\n        {facility_content}"
            user_message += " "
        else:
            user_message = f"""사용자 질문: {message}
        retrieved_documents:
        {doc_content}"""
            if facility_content:
                user_message += f"\n        retrieved_facilities:\n        {facility_content}"
            user_message += " "

        if messages:
            history = [m for m in messages[:-1] if m.get("role") in (ROLE_USER, "assistant")]
            # LLM은 user 메시지로 시작해야 함 — 선두 assistant 메시지(초기 인사말 등) 제거
            while history and history[0].get("role") == ROLE_ASSISTANT:
                history = history[1:]
            final_messages = history + [{"role": ROLE_USER, "content": user_message}]
        else:
            final_messages = [{"role": ROLE_USER, "content": user_message}]
        final_messages = truncate_messages_by_token_limit(final_messages)

        logger.debug("[Final Response] system_prompt:\n%s", system_prompt)
        logger.debug("[Final Response] messages:\n%s", final_messages)

        response = await call_llm_api(
            temperature=temperature,
            max_tokens=max_tokens,
            messages=final_messages,
            extra_system_prompts=[system_prompt],
            stream=stream,
            frequency_penalty=frequency_penalty,
            repetition_penalty=repetition_penalty,
            top_p=top_p,
            top_k=top_k,
            seed=seed,
            tools=tools
        )

        logger.info("[Final Response] 응답 생성 완료")
        return response

    except Exception as e:
        logger.error(f"[Final Response] 오류: {e}", exc_info=True)
        raise
