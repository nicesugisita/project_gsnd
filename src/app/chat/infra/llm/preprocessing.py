"""쿼리 전처리 및 음성 변환"""

import logging

from app.core.constants import ROLE_USER, TTS_VOICE_CLEAN_MAX_TOKENS
from app.core.exceptions import LLMServiceError
from app.shared.utils.helpers import shorten_text
from app.shared.utils.prompt_loader import (
    load_convert_korean_prompt,
    load_voice_print_before_prompt,
)
from app.chat.infra.deepserver.client import (
    deepserver_clean_text_query,
    deepserver_clean_voice_query,
    deepserver_classify_general_or_care,
)

from .core import call_llm_api

logger = logging.getLogger(__name__)


async def classify_general_or_care(user_query: str) -> str:
    """
    사용자 질의가 일반(GENERAL)인지 복지(WELFARE)인지 분류

    Returns:
        'GENERAL' or 'WELFARE'
    """
    try:
        result = (await deepserver_classify_general_or_care(user_query)).strip()
        if result in ("GENERAL", "WELFARE"):
            logger.info(f"[Intent Classification] {user_query[:50]} => {result}")
            return result
        logger.warning(f"[Intent Classification] Unexpected result: {result!r} → WELFARE fallback")
        return "WELFARE"
    except Exception as e:
        logger.error(f"[Intent Classification] Error: {e}", exc_info=True)
        return "WELFARE"


async def clean_query_text(user_query: str, messages: list = None, input_type: str = "text") -> str:
    """
    질의 텍스트 정제 함수

    Args:
        user_query: 사용자 질문
        messages: 메시지 히스토리 (옵션)
        input_type: 입력 타입 ('text' 또는 'voice')

    Returns:
        정제된 질문
    """
    try:
        if input_type == "voice":
            log_prefix = "[VOICE CLEANING]"
            response = await deepserver_clean_voice_query(user_query)
        else:
            log_prefix = "[TEXT CLEANING]"
            response = await deepserver_clean_text_query(user_query)

        cleaned_text = response.strip()
        if cleaned_text:
            logger.info(
                "%s Original: %s | Cleaned: %s",
                log_prefix,
                shorten_text(user_query, 50),
                shorten_text(cleaned_text, 50)
            )
            return cleaned_text

        return user_query

    except Exception as e:
        logger.error(f"Error cleaning query text: {e}", exc_info=True)
        return user_query


async def convert_to_voice_output(answer_text: str) -> str:
    """
    최종 응답을 TTS 음성 출력에 적합한 형태로 변환

    Args:
        answer_text: LLM이 생성한 최종 응답 텍스트

    Returns:
        음성 출력에 적합하게 변환된 텍스트
    """
    try:
        voice_prompt = load_voice_print_before_prompt()
        if not voice_prompt:
            logger.warning("[VOICE OUTPUT] Voice print prompt not found, returning original text")
            return answer_text

        call_messages = [{"role": ROLE_USER, "content": f"다음 텍스트를 TTS용 스크립트로 변환하세요:\n\n<변환할_텍스트>\n{answer_text}\n</변환할_텍스트>"}]

        response = await call_llm_api(
            temperature=0,
            messages=call_messages,
            extra_system_prompts=[voice_prompt],
            max_tokens=TTS_VOICE_CLEAN_MAX_TOKENS
        )

        if isinstance(response, str):
            converted_text = response.strip()
            if converted_text:
                logger.info(
                    "[VOICE OUTPUT] Original length: %d | Converted length: %d",
                    len(answer_text),
                    len(converted_text)
                )
                logger.debug(
                    "[VOICE OUTPUT] Original: %s | Converted: %s",
                    shorten_text(answer_text, 100),
                    shorten_text(converted_text, 100)
                )
                return converted_text

        return answer_text

    except Exception as e:
        logger.error(f"[VOICE OUTPUT] Error converting to voice output: {e}", exc_info=True)
        return answer_text


async def convert_korean_to_standard(user_query: str) -> str:
    """
    사용자 질문을 표준어로 변환

    Args:
        user_query: 원본 사용자 질문

    Returns:
        표준어로 변환된 질문 (실패 시 원본 반환)
    """
    if not user_query or not user_query.strip():
        return user_query

    try:
        prompt = load_convert_korean_prompt()
        if not prompt:
            logger.warning("[Convert Korean] 프롬프트 로드 실패")
            return user_query

        system_prompt = prompt.replace("{질문}", user_query)

        response = await call_llm_api(
            temperature=0,
            messages=[{"role": ROLE_USER, "content": user_query}],
            extra_system_prompts=[system_prompt]
        )

        if isinstance(response, str) and response.strip():
            logger.info("[Convert Korean] 변환 완료: %s -> %s", user_query[:50], response[:50])
            return response.strip()

        logger.warning("[Convert Korean] 빈 응답 수신")
        return user_query

    except LLMServiceError as e:
        logger.warning(f"[Convert Korean] LLM 오류: {e}")
        return user_query
    except Exception as e:
        logger.error(f"[Convert Korean] 예상치 못한 오류: {e}", exc_info=True)
        return user_query
