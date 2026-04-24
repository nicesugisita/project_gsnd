"""사용자가 추가 결과를 요청하는지 감지하고, 이전에 보여준 문서의 chunk_id와 서비스명을 추출합니다."""

from typing import List, Tuple
from core.constants import ROLE_ASSISTANT

MORE_RESULTS_PATTERNS = (
    "더 알려줘",
    "더 보여줘",
    "더 있나요",
    "더 있어요",
    "추가로 알려줘",
    "다른 것도",
    "또 있나요",
    "더 알고 싶어",
    "더 알고싶어",
    "다른 서비스",
    "또 다른",
    "그 외에",
    "더 없나요",
)


def is_more_results_intent(user_message: str) -> bool:
    """사용자가 추가 결과를 요청하는지 감지."""
    msg = user_message.strip()
    return any(p in msg for p in MORE_RESULTS_PATTERNS)


def get_excluded_info_from_history(messages: list) -> Tuple[List[str], List[str]]:
    """대화 이력에서 직전 assistant 응답의 chunk_ids와 서비스명을 추출.

    Returns:
        (excluded_chunk_ids, excluded_service_names)
    """
    for msg in reversed(messages):
        if msg.get("role") == ROLE_ASSISTANT:
            chunk_ids = [str(i) for i in msg.get("referenced_chunk_ids", []) if i]
            names = [str(n) for n in msg.get("referenced_service_names", []) if n]
            return chunk_ids, names
    return [], []
