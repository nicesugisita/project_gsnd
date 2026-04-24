"""
생애주기 확인 및 되묻기 서비스

guide_recommend 의도에서 사용자의 생애주기(영유아/아동/청소년/청년/중장년/노인)를
대화 히스토리에서 추출하고, 확인되지 않은 경우 되묻기 처리를 담당합니다.
"""

import logging
from typing import Dict, List, Tuple
from core.constants import ROLE_ASSISTANT

logger = logging.getLogger(__name__)

# ============================================================================
# 상수
# ============================================================================

# 생애주기 라벨 목록
LIFECYCLE_LABELS: List[str] = ["영유아", "아동", "청소년", "청년", "중장년", "노인"]

# 되묻기 감지용 마커
_MARKER_LIFECYCLE_ASK = "어떤 대상에 해당하시나요"

# 되묻기 메시지
MSG_ASK_LIFECYCLE = (
    "어떤 대상에 해당하시나요? "
    "('영유아', '아동', '청소년', '청년', '중장년', '노인' 중 선택해 주세요.)"
)

# 최대 되묻기 횟수
MAX_LIFECYCLE_ASK_ATTEMPTS = 2

# 생애주기 되묻기 면제 키워드 — 장애·질환 등 나이와 무관한 명확한 대상
_SKIP_LIFECYCLE_KEYWORDS = {
    "장애", "장애인", "지적장애", "신체장애", "발달장애", "정신장애",
    "청각장애", "시각장애", "지체장애", "뇌병변", "자폐",
    "장애등급", "장애 등급", "장애인복지",
    "희귀질환", "난치질환", "중증질환", "만성질환",
}


# ============================================================================
# 공개 API
# ============================================================================

def extract_lifecycle_from_history(messages: List[Dict]) -> str:
    """
    전체 대화 히스토리(user 메시지)에서 생애주기를 추출합니다.

    최신 메시지부터 역순으로 검색합니다.

    Returns:
        생애주기 문자열 (예: "노인") 또는 ""
    """
    # 지연 임포트: 순환 참조 방지
    from services.rag_service import (
        _extract_lifecycle_from_message,
        _extract_birth_year_from_message,
        _birth_year_to_lifecycle,
    )

    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")

        # 나이/출생연도 기반 추출
        birth_year = _extract_birth_year_from_message(content)
        if birth_year:
            lc = _birth_year_to_lifecycle(birth_year)
            logger.info(f"[LifecycleService] 출생연도 {birth_year} → 생애주기: '{lc}'")
            return lc

        # 키워드 직접 추출
        lifecycle = _extract_lifecycle_from_message(content)
        if lifecycle:
            logger.info(f"[LifecycleService] 키워드 추출 생애주기: '{lifecycle}'")
            return lifecycle

    return ""


def count_lifecycle_ask_attempts(messages: List[Dict]) -> int:
    """대화 히스토리에서 생애주기 되묻기 횟수를 셉니다."""
    return sum(
        1 for m in messages
        if m.get("role") == ROLE_ASSISTANT and _MARKER_LIFECYCLE_ASK in m.get("content", "")
    )


def check_lifecycle(
    user_message: str,
    messages: List[Dict],
) -> Tuple[str, bool, str]:
    """
    생애주기 추출 및 되묻기 여부를 판단합니다.

    판단 순서:
    1. 전체 히스토리(user 메시지)에서 생애주기 추출 → 있으면 확정
    2. 없으면 되묻기 필요

    Returns:
        (lifecycle, need_clarify, ask_message)
        - lifecycle: 확인된 생애주기 (예: "노인"), 없으면 ""
        - need_clarify: 되묻기 필요 여부
        - ask_message: 되묻기 메시지 (need_clarify=True 일 때만 유효)
    """
    logger.info(
        f"[check_lifecycle] 호출됨 | query={user_message[:40]!r} | messages 수={len(messages)}"
    )

    lifecycle = extract_lifecycle_from_history(messages)
    if lifecycle:
        logger.info(f"[LifecycleService] 생애주기 확인(history): '{lifecycle}'")
        return lifecycle, False, ""

    # messages에 현재 메시지가 미반영된 경우를 대비해 user_message 직접 확인
    from services.rag_service import (
        _extract_lifecycle_from_message,
        _extract_birth_year_from_message,
        _birth_year_to_lifecycle,
    )
    birth_year = _extract_birth_year_from_message(user_message)
    if birth_year:
        lc = _birth_year_to_lifecycle(birth_year)
        logger.info(f"[LifecycleService] 현재 메시지 출생연도 {birth_year} → 생애주기: '{lc}'")
        return lc, False, ""

    lifecycle = _extract_lifecycle_from_message(user_message)
    if lifecycle:
        logger.info(f"[LifecycleService] 현재 메시지 키워드 → 생애주기: '{lifecycle}'")
        return lifecycle, False, ""

    # 장애·질환 관련 질문은 생애주기와 무관 → 되묻기 없이 통과
    all_user_text = " ".join(
        m.get("content", "") for m in messages if m.get("role") == "user"
    ) + " " + user_message
    if any(kw in all_user_text for kw in _SKIP_LIFECYCLE_KEYWORDS):
        logger.info("[LifecycleService] 장애/질환 키워드 감지 → 생애주기 되묻기 면제")
        return "", False, ""

    logger.info("[LifecycleService] 생애주기 미확인 → 되묻기")
    return "", True, MSG_ASK_LIFECYCLE
