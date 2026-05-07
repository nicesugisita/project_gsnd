import json
import logging
from typing import Iterable, Optional

from app.chat.infra.deepserver.client import _call_generation

logger = logging.getLogger(__name__)

RE_QUERY_PROMPT_ID = 21


def _has_assistant_history(messages: Optional[Iterable[dict]]) -> bool:
    """이전 assistant 응답이 하나라도 있으면 True (첫 턴 판정용)."""
    if not messages:
        return False
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant" and str(m.get("content") or "").strip():
            return True
    return False


async def reform_query_if_needed(
    user_message: str,
    messages: Optional[list],
) -> str:
    """첫 턴(assistant 히스토리 없음)이면 reform LLM을 건너뛰고 user_message 그대로 반환.

    멀티턴(이전 대화가 있는 경우)에서만 reform_query_with_history를 호출하여
    \"더 알려줘\", \"그것\" 같은 컨텍스트 의존 질의를 보정한다.
    실패/빈 결과 시 원본 user_message로 폴백.
    """
    if not _has_assistant_history(messages):
        logger.info("[reform_query_if_needed] 첫 턴 감지 → reform 생략 (user_message 사용)")
        return user_message
    try:
        return await reform_query_with_history(
            user_message=user_message, messages=messages or []
        ) or user_message
    except Exception as e:  # noqa: BLE001 - 폴백으로 원본 사용
        logger.warning(
            "[reform_query_if_needed] reform 실패 → user_message 폴백: %s", e
        )
        return user_message


async def reform_query_with_history(user_message: str, messages: list):
    # 최근 6개 대화 이력을 QUESTION에 포함
    history = messages[-6:]
    history_text = "\n".join(
        f"{m.get('role')}: {m.get('content')}" for m in history
    )
    question = f"[대화 이력]\n{history_text}\n\n[현재 질문]{user_message}"

    logger.info(f"[reform_query_with_history] history: {history}")
    logger.info(f"[reform_query_with_history] question: {question}")

    try:
        output = await _call_generation(question, RE_QUERY_PROMPT_ID, task="")
        logger.info(f"[reform_query_with_history] output: {output}")

        parsed = json.loads(output)
        search_query = parsed.get("search_query", "").strip() if isinstance(parsed, dict) else ""
        if search_query:
            logger.info(f"[reform_query_with_history] result: {search_query}")
            return search_query

        logger.warning("[reform_query_with_history] search_query 키 없음, 원본 메시지 반환")
        return user_message

    except Exception as e:
        logger.error(f"[reform_query_with_history] error: {e}")
        return user_message
