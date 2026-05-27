"""정보량 0 입력 감지 및 분야 되묻기 서비스.

`알려줘`, `뭐 있어`, `복지 정보` 처럼 사용자가 원하는 복지 분야·대상이
전혀 드러나지 않은 원질문에 대해, 시군 확정 이후 검색에 들어가기 전
회차별로 다른 각도(분야 → 상황 → 키워드)로 되묻는다.

설계 의도:
- 시군이 확정된 직후(`is_clarification=True`)에만 호출 — 첫 발화에서
  시군 되묻기와 동시에 묻지 않음(되묻기 누적·UX 부담 방지).
- 1~3회차는 다른 정보 축(분야 → 상황 → 키워드+129)을 묻고,
  4회차 이후엔 같은 fallback 메시지를 반복한다(무관한 RAG 결과
  강제 노출보다 명확한 dead-end 신호가 낫다).
- 사용자가 정보 있는 답변(`is_low_info_query=False`)을 주면 어느
  turn에서든 즉시 정상 RAG 진입.
"""

from typing import List, Dict, Tuple

from app.core.constants import ROLE_ASSISTANT
from app.shared.utils.keyword_extractor import extract_nouns


# 회차별 되묻기 메시지 — 매번 다른 정보 축을 묻는다.
TOPIC_ASK_MESSAGES: List[str] = [
    # 1회차: 분야 카테고리
    "어떤 복지 분야에 대해 알려드릴까요? "
    "(예: 청년 일자리, 노인 돌봄, 장애 지원, 출산·보육)",
    # 2회차: 대상·상황
    "어떤 상황에 해당하시나요? "
    "(예: 65세 이상 어르신, 임산부, 한부모 가정, 장애가 있으신 분)",
    # 3회차: 키워드 한 단어 유도 + 행정복지센터 fallback 안내
    "찾으시는 도움을 한 단어로 알려주시면 안내드릴 수 있습니다. "
    "(예: 의료비, 일자리, 주거, 양육)\n"
    "이번에도 답변이 어려우시면 가까운 읍면동 행정복지센터"
    "(국번 없이 129)에 문의해 보세요.",
]

# 4회차 이후 반복되는 fallback 메시지 — 정보량 0 답변이 계속될 때
# 같은 메시지를 반복해 "여기서는 안내가 어렵다"는 명확한 dead-end 신호를 준다.
TOPIC_ASK_FALLBACK_REPEAT: str = (
    "정확한 안내가 어려워 가까운 읍면동 행정복지센터(국번 없이 129)에 "
    "문의 부탁드립니다. 또는 찾으시는 도움을 한 단어로 알려주세요."
)

# 외부 import 호환: 1회차 메시지를 기본 상수로 노출
MSG_ASK_TOPIC: str = TOPIC_ASK_MESSAGES[0]

# 누적 카운트용 마커 패턴 — 회차별 메시지의 고유 prefix + fallback 마커.
# 어떤 메시지든 하나라도 포함되어 있으면 한 번의 시도로 카운트.
_MARKER_PATTERNS: Tuple[str, ...] = (
    "어떤 복지 분야에 대해 알려드릴까요",
    "어떤 상황에 해당하시나요",
    "찾으시는 도움을 한 단어로",
    "정확한 안내가 어려워",  # fallback 마커
)
# 외부 import 호환
_MARKER_TOPIC_ASK: str = _MARKER_PATTERNS[0]

# 호환성 유지용 — 새 로직에서는 cap이 없다(정보 0 답변이 계속되면 fallback 반복).
# 외부 import 한 곳에서는 "회차 변동 메시지의 풀 크기" 의미로 사용 가능.
MAX_TOPIC_ASK_ATTEMPTS: int = len(TOPIC_ASK_MESSAGES)

# 토픽으로 인정하지 않는 일반어. 이 단어만 등장하면 사용자 의도가 좁혀지지 않은 것으로 본다.
_GENERIC_NOUNS: frozenset = frozenset({
    "복지", "서비스", "정보", "지원", "사업", "제도", "혜택", "안내", "추천",
})


def count_topic_ask_attempts(messages: List[Dict]) -> int:
    """대화 히스토리에서 분야 되묻기 횟수를 셉니다.

    회차별로 마커 문구가 다르므로 `_MARKER_PATTERNS` 중 어느 하나라도
    포함되면 한 번의 시도로 카운트. fallback 메시지도 카운트되어
    attempts 가 4, 5, 6, ... 로 무한히 증가할 수 있다.
    """
    return sum(
        1 for m in messages
        if m.get("role") == ROLE_ASSISTANT
        and any(p in m.get("content", "") for p in _MARKER_PATTERNS)
    )


def last_assistant_is_topic_ask(messages: List[Dict]) -> bool:
    """가장 최근 assistant 메시지가 topic_clarify 회차 메시지(또는 fallback)인지.

    `is_clarification_answer` 가 '?'로 끝나는지만 보기 때문에 topic_clarify
    메시지가 ')'/'.'로 끝나면 후속 turn이 게이트에서 막힌다. 이 헬퍼로
    호출자가 게이트를 우회할 수 있다.
    """
    for m in reversed(messages):
        if m.get("role") == ROLE_ASSISTANT:
            content = m.get("content", "")
            return any(p in content for p in _MARKER_PATTERNS)
    return False


def is_low_info_query(query: str) -> bool:
    """원질문에 분야를 좁힐 수 있는 명사가 하나도 없으면 True.

    판단 규칙:
    - 빈 문자열 / 공백뿐 → True
    - kiwi 명사 추출 후 `_GENERIC_NOUNS` 제외한 토픽 명사가 0개 → True
    - 그 외 → False
    """
    text = (query or "").strip()
    if not text:
        return True
    try:
        nouns = [n.strip() for n in extract_nouns(text, use_bigram=False) if n and n.strip()]
    except Exception:
        nouns = []
    topic_nouns = [n for n in nouns if n not in _GENERIC_NOUNS]
    return len(topic_nouns) == 0


def _select_topic_ask_message(attempts: int) -> str:
    """회차 attempts(0-based)에 맞는 메시지 선택.

    풀 범위(0~len-1)는 회차별 메시지, 그 이후는 fallback 메시지 반복.
    """
    if attempts < len(TOPIC_ASK_MESSAGES):
        return TOPIC_ASK_MESSAGES[attempts]
    return TOPIC_ASK_FALLBACK_REPEAT


def check_topic_clarification(
    original_user_question: str,
    messages: List[Dict],
) -> Tuple[bool, str]:
    """분야 되묻기가 필요한지 판정하고, 현재 시도 횟수에 맞는 메시지를 반환.

    Args:
        original_user_question: 판정 대상 사용자 발화.
            - 시군 되묻기 직후 첫 호출: 원질문(예: "알려줘")
            - topic_clarify 후속 turn: 현재 user 메시지(예: 또 "알려줘")
        messages: 현재 대화 이력 (현재 user 메시지 포함).

    Returns:
        (need_ask, ask_message)
        - 사용자가 정보 있는 답변(`is_low_info_query=False`)을 주면 (False, "")
          → 즉시 정상 RAG 진입
        - 정보량 0이면 항상 (True, <회차 메시지 또는 fallback>) — cap 없음
    """
    if not is_low_info_query(original_user_question):
        return False, ""
    attempts = count_topic_ask_attempts(messages)
    return True, _select_topic_ask_message(attempts)
