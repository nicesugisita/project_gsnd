"""토큰 관리 유틸리티"""

import logging
from typing import Dict, List

import tiktoken

from app.core.constants import ROLE_SYSTEM

logger = logging.getLogger(__name__)

try:
    _TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception as e:
    logger.warning(f"Tiktoken 인코더 로드 실패: {e}")
    _TIKTOKEN_ENCODER = None


def truncate_messages_by_token_limit(
    messages: List[Dict[str, str]],
    max_tokens: int = 32000,
    reserved_tokens: int = 3000
) -> List[Dict[str, str]]:
    """
    토큰 제한에 따라 메시지 목록 길이 제한

    시스템 프롬프트, 문서 등의 추가 컨텍스트를 위해 일부 토큰을 예약하고,
    나머지 토큰 범위 내에서 메시지 히스토리를 유지합니다.

    Args:
        messages: 메시지 목록
        max_tokens: LLM 최대 토큰 수
        reserved_tokens: 시스템 프롬프트/문서 등을 위해 남길 토큰 수

    Returns:
        토큰 제한을 고려한 메시지 목록
    """
    if not messages or _TIKTOKEN_ENCODER is None:
        return messages

    total_tokens = 0
    result = []

    system_msgs = [m for m in messages if m.get("role") == ROLE_SYSTEM]
    other_msgs = [m for m in messages if m.get("role") != ROLE_SYSTEM]

    for msg in reversed(other_msgs):
        try:
            tokens = len(_TIKTOKEN_ENCODER.encode(msg.get("content", "")))
            if total_tokens + tokens > max_tokens - reserved_tokens:
                break
            result.append(msg)
            total_tokens += tokens
        except Exception as e:
            logger.warning(f"메시지 토큰 계산 실패: {e}")
            result.append(msg)

    result = list(reversed(result))
    return system_msgs + result


def _truncate_text_by_tokens(text: str, max_tokens: int) -> str:
    """
    텍스트를 최대 토큰 수 기준으로 잘라 반환

    Args:
        text: 원본 텍스트
        max_tokens: 허용 최대 토큰 수

    Returns:
        잘린 텍스트 (필요 시)
    """
    if not text or max_tokens <= 0:
        return ""

    if _TIKTOKEN_ENCODER is None:
        approx_chars = max_tokens * 3
        return text if len(text) <= approx_chars else text[:approx_chars]

    tokens = _TIKTOKEN_ENCODER.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return _TIKTOKEN_ENCODER.decode(tokens[:max_tokens])
