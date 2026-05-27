"""topic_clarify 모듈 단위 테스트.

정보량 0 원질문 감지 + 회차별 progressive 분야 되묻기 카운트 + 통합 판정 검증.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import pytest

from app.chat.topic_clarify import (
    MSG_ASK_TOPIC,
    MAX_TOPIC_ASK_ATTEMPTS,
    TOPIC_ASK_MESSAGES,
    TOPIC_ASK_FALLBACK_REPEAT,
    check_topic_clarification,
    count_topic_ask_attempts,
    is_low_info_query,
    last_assistant_is_topic_ask,
)


# ============================================================
# is_low_info_query
# ============================================================

@pytest.mark.parametrize("query", [
    "",
    "   ",
    "알려줘",
    "알려 주세요",
    "뭐 있어",
    "뭐 있나요",
    "추천해줘",
    "복지",
    "복지 정보",
    "복지 서비스",
    "지원 사업",
    "복지 정보 알려줘",
    "?",
])
def test_low_info_query_detects_generic(query):
    """일반어·동사만 있거나 빈 입력은 low-info로 판정."""
    assert is_low_info_query(query) is True, f"low-info로 판정돼야 함: {query!r}"


@pytest.mark.parametrize("query", [
    "기초연금 알려줘",
    "노인 일자리",
    "청년 월세 지원",
    "장애인 복지",
    "한부모 가족 지원",
    "치매 검사비",
    "임산부 교통비",
    "창원 노인 돌봄",
    "출산 지원금",
])
def test_low_info_query_passes_topic_words(query):
    """구체적인 대상/제도/분야가 들어가면 low-info 아님."""
    assert is_low_info_query(query) is False, f"low-info가 아니어야 함: {query!r}"


# ============================================================
# 메시지 풀 invariant
# ============================================================

def test_message_pool_invariant():
    """메시지 풀 길이와 MAX_TOPIC_ASK_ATTEMPTS 가 일치해야 한다."""
    assert len(TOPIC_ASK_MESSAGES) == MAX_TOPIC_ASK_ATTEMPTS
    assert MSG_ASK_TOPIC == TOPIC_ASK_MESSAGES[0]


def test_third_message_contains_fallback():
    """3회차 메시지에는 행정복지센터(129) fallback 안내가 포함된다."""
    third = TOPIC_ASK_MESSAGES[2]
    assert "행정복지센터" in third
    assert "129" in third


# ============================================================
# count_topic_ask_attempts
# ============================================================

def test_count_topic_ask_attempts_empty():
    assert count_topic_ask_attempts([]) == 0


def test_count_topic_ask_attempts_counts_first_marker():
    messages = [
        {"role": "user", "content": "알려줘"},
        {"role": "assistant", "content": "어느 시군에 거주하고 계신가요?"},
        {"role": "user", "content": "창원"},
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[0]},
        {"role": "user", "content": "알려줘"},
    ]
    assert count_topic_ask_attempts(messages) == 1


def test_count_topic_ask_attempts_counts_all_three_rounds():
    """회차별로 다른 마커가 들어간 메시지들도 모두 카운트."""
    messages = [
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[0]},
        {"role": "user", "content": "알려줘"},
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[1]},
        {"role": "user", "content": "잘 모르겠어"},
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[2]},
    ]
    assert count_topic_ask_attempts(messages) == 3


def test_count_topic_ask_attempts_ignores_user_messages():
    """user 메시지에 같은 문구가 우연히 있어도 카운트하지 않음."""
    messages = [
        {"role": "user", "content": MSG_ASK_TOPIC},
    ]
    assert count_topic_ask_attempts(messages) == 0


# ============================================================
# check_topic_clarification (통합) — 회차별 메시지 진행
# ============================================================

def test_check_first_attempt_returns_field_question():
    """1회차: 분야 카테고리 질문."""
    messages = [
        {"role": "user", "content": "알려줘"},
        {"role": "assistant", "content": "어느 시군에 거주하고 계신가요?"},
        {"role": "user", "content": "창원"},
    ]
    need_ask, msg = check_topic_clarification("알려줘", messages)
    assert need_ask is True
    assert msg == TOPIC_ASK_MESSAGES[0]
    assert "복지 분야" in msg


def test_check_second_attempt_returns_situation_question():
    """1회차 묻고 또 정보량 0 → 2회차: 상황 질문."""
    messages = [
        {"role": "user", "content": "알려줘"},
        {"role": "assistant", "content": "어느 시군에 거주하고 계신가요?"},
        {"role": "user", "content": "창원"},
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[0]},
        {"role": "user", "content": "알려줘"},
    ]
    need_ask, msg = check_topic_clarification("알려줘", messages)
    assert need_ask is True
    assert msg == TOPIC_ASK_MESSAGES[1]
    assert "상황에 해당" in msg


def test_check_third_attempt_returns_keyword_with_fallback():
    """2회차까지 묻고 또 정보량 0 → 3회차: 키워드 + fallback 안내."""
    messages = [
        {"role": "user", "content": "알려줘"},
        {"role": "assistant", "content": "어느 시군에 거주하고 계신가요?"},
        {"role": "user", "content": "창원"},
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[0]},
        {"role": "user", "content": "알려줘"},
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[1]},
        {"role": "user", "content": "잘 모르겠어"},
    ]
    need_ask, msg = check_topic_clarification("잘 모르겠어", messages)
    assert need_ask is True
    assert msg == TOPIC_ASK_MESSAGES[2]
    assert "한 단어" in msg
    assert "행정복지센터" in msg
    assert "129" in msg


def test_check_topic_word_skips_ask():
    """원질문에 구체적 분야가 있으면 묻지 않는다."""
    messages = [
        {"role": "user", "content": "기초연금 알려줘"},
        {"role": "assistant", "content": "어느 시군에 거주하고 계신가요?"},
        {"role": "user", "content": "창원"},
    ]
    need_ask, msg = check_topic_clarification("기초연금 알려줘", messages)
    assert need_ask is False
    assert msg == ""


def test_check_fourth_attempt_returns_fallback_repeat():
    """3회 모두 시도하고도 정보량 0이면 fallback 메시지 반복(cap 없음)."""
    messages = [
        {"role": "user", "content": "알려줘"},
        {"role": "assistant", "content": "어느 시군에 거주하고 계신가요?"},
        {"role": "user", "content": "창원"},
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[0]},
        {"role": "user", "content": "알려줘"},
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[1]},
        {"role": "user", "content": "몰라"},
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[2]},
        {"role": "user", "content": "그냥"},   # 4회차 입력도 정보량 0
    ]
    assert count_topic_ask_attempts(messages) == 3
    need_ask, msg = check_topic_clarification("그냥", messages)
    assert need_ask is True, "정보량 0이면 cap 없이 계속 묻는다"
    assert msg == TOPIC_ASK_FALLBACK_REPEAT
    assert "129" in msg


def test_check_fifth_attempt_keeps_returning_fallback():
    """4회차 fallback 후에도 정보 0이면 동일 fallback 반복."""
    messages = [
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[0]},
        {"role": "user", "content": "알려줘"},
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[1]},
        {"role": "user", "content": "몰라"},
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[2]},
        {"role": "user", "content": "그냥"},
        {"role": "assistant", "content": TOPIC_ASK_FALLBACK_REPEAT},
        {"role": "user", "content": "음"},   # 5회차 입력도 정보량 0
    ]
    # fallback도 카운트되어 attempts=4
    assert count_topic_ask_attempts(messages) == 4
    need_ask, msg = check_topic_clarification("음", messages)
    assert need_ask is True
    assert msg == TOPIC_ASK_FALLBACK_REPEAT


def test_check_info_answer_breaks_loop_anytime():
    """어느 turn에서든 정보 있는 답변을 주면 즉시 정상 진행."""
    messages = [
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[0]},
        {"role": "user", "content": "알려줘"},
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[1]},
        {"role": "user", "content": "몰라"},
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[2]},
        {"role": "user", "content": "노인 일자리"},   # 정보 있음
    ]
    need_ask, msg = check_topic_clarification("노인 일자리", messages)
    assert need_ask is False, "정보 답변이면 fallback 루프 즉시 종료"
    assert msg == ""


# ============================================================
# last_assistant_is_topic_ask (게이트 우회용 헬퍼)
# ============================================================

def test_last_assistant_is_topic_ask_detects_all_rounds():
    """1·2·3회차 메시지 + fallback 모두 마커로 인식."""
    for marker_msg in (*TOPIC_ASK_MESSAGES, TOPIC_ASK_FALLBACK_REPEAT):
        messages = [
            {"role": "user", "content": "알려줘"},
            {"role": "assistant", "content": marker_msg},
        ]
        assert last_assistant_is_topic_ask(messages) is True, f"감지 실패: {marker_msg[:30]}"


def test_last_assistant_is_topic_ask_false_for_other_messages():
    """다른 assistant 메시지(시군 되묻기 등)는 False."""
    messages = [
        {"role": "user", "content": "알려줘"},
        {"role": "assistant", "content": "어느 시군에 거주하고 계신가요?"},
    ]
    assert last_assistant_is_topic_ask(messages) is False


def test_last_assistant_is_topic_ask_only_checks_latest():
    """가장 최근 assistant 만 본다 — 이전에 topic_ask 가 있었어도 최신이 다르면 False."""
    messages = [
        {"role": "assistant", "content": TOPIC_ASK_MESSAGES[0]},
        {"role": "user", "content": "창원 노인 일자리"},
        {"role": "assistant", "content": "노인 일자리 안내드립니다..."},  # 일반 답변
        {"role": "user", "content": "더 알려줘"},
    ]
    assert last_assistant_is_topic_ask(messages) is False


def test_check_empty_query_triggers_ask():
    messages = [{"role": "user", "content": ""}]
    need_ask, _ = check_topic_clarification("", messages)
    assert need_ask is True
