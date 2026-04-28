import json
import logging
from app.chat.infra.deepserver.client import _call_generation

logger = logging.getLogger(__name__)

RE_QUERY_PROMPT_ID = 21


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
