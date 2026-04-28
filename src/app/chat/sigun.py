"""
시군 추출 및 되묻기 서비스

사용자 질의와 대화 히스토리에서 경상남도 시·군을 추출하고,
시군이 확인되지 않을 경우 되묻기 처리를 담당합니다.

주요 기능:
- 대화 히스토리 전체에서 시군 추출 (히스토리 기반 기억)
- 읍/면/동/리명 → 시군 매핑 (location_dict 활용)
- 조사/경계 기반 정확한 위치명 매칭 (오탐 방지)
- 모호한 위치명(여러 시군 해당) 감지 및 되묻기
- 시군 없음 감지 및 되묻기
- 되묻기 3회 초과 시 포기 메시지
"""

import re
import logging
import os
from typing import List, Tuple, Optional, Dict

from app.core.constants import ROLE_ASSISTANT
from app.mariner.sigun_utils import normalize_sigun

logger = logging.getLogger(__name__)

# ============================================================================
# location_dict 로드 (location_dict.json)
# ============================================================================
import json

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JSON_PATH = os.path.join(_project_root, "location_dict.json")

try:
    with open(_JSON_PATH, encoding="utf-8") as _f:
        _location_dict: Dict[str, list] = json.load(_f)
    logger.info(f"[SigunService] location_dict 로드 완료: {len(_location_dict)}개 키")
except FileNotFoundError:
    logger.warning(f"[SigunService] {_JSON_PATH} 없음 — sigun.py를 먼저 실행하세요")
    _location_dict = {}
except Exception as _e:
    logger.warning(f"[SigunService] location_dict 로드 실패: {_e} — 읍/면/동/리 매칭 비활성화")
    _location_dict = {}

# ============================================================================
# 상수
# ============================================================================

# 시군 되묻기 감지용 마커 (히스토리에서 되묻기 횟수 카운트에 사용)
_MARKER_NO_REGION = "어느 지역에 거주하고 계신가요"
_MARKER_AMBIGUOUS = "어디에 해당하시나요"

# 시군이 없을 때 되묻기 메시지
MSG_ASK_NO_REGION = "경상남도 내 시/군을 알려주세요. 어느 지역에 거주하고 계신가요?"

# 시군 되묻기 3회 초과 시 포기 메시지
MSG_SIGUN_FAILURE = (
    "거주중인 지역을 확인할 수 없어 정확한 답변을 드릴 수 없습니다. "
    "지역 확인 후 다시 질문 주세요."
)

# 최대 되묻기 횟수
MAX_SIGUN_ASK_ATTEMPTS = 3

# 경상남도 외 지역 → 표시명 매핑 (긴 이름 우선 정렬로 부분매칭 방지)
_OUT_OF_SCOPE_REGION_MAP: Dict[str, str] = {
    "서울특별시": "서울", "서울시": "서울", "서울": "서울",
    "부산광역시": "부산", "부산시": "부산", "부산": "부산",
    "대구광역시": "대구", "대구시": "대구", "대구": "대구",
    "인천광역시": "인천", "인천시": "인천", "인천": "인천",
    "광주광역시": "광주", "광주시": "광주", "광주": "광주",
    "대전광역시": "대전", "대전시": "대전", "대전": "대전",
    "울산광역시": "울산", "울산시": "울산", "울산": "울산",
    "세종특별자치시": "세종", "세종시": "세종", "세종": "세종",
    "경기도": "경기도", "경기": "경기",
    "강원특별자치도": "강원도", "강원도": "강원도", "강원": "강원",
    "충청북도": "충청북도", "충북": "충북",
    "충청남도": "충청남도", "충남": "충남",
    "전북특별자치도": "전라북도", "전라북도": "전라북도", "전북": "전북",
    "전라남도": "전라남도", "전남": "전남",
    "경상북도": "경상북도", "경북": "경북",
    "제주특별자치도": "제주도", "제주도": "제주도", "제주": "제주",
}
# 긴 키를 먼저 매칭하여 부분 오탐 방지
_OUT_OF_SCOPE_KEYS_SORTED = sorted(_OUT_OF_SCOPE_REGION_MAP, key=len, reverse=True)

MSG_OUT_OF_SCOPE_TEMPLATE = (
    "본 안내는 경상남도 지역의 복지 서비스에 대한 정보만 제공합니다. "
    "{region}에서 시행 중인 복지 서비스에 대한 정보는 제공할 수 없습니다."
)

# 위치명 최소 글자 수 (오탐 방지)
_MIN_KEY_LEN = 2

# 위치명 뒤에 허용되는 조사/구두점/공백 (lookahead)
# 예: "가산리에서", "가산리 복지", "가산리요", "가산리!" 등 매칭
_PARTICLE_LOOKAHEAD = (
    r'(?='
    r'[에의이가은는을를로으로와과부터까지도요야죠네,.\s?!」』\"\']'
    r'|인데|이에요|예요|이요'
    r'|$)'
)


# ============================================================================
# 내부 헬퍼
# ============================================================================

def _match_location_key(key: str, text: str) -> bool:
    """
    location_dict 키(읍/면/동/리명)가 text에서 조사/경계 조건을 만족하는지 확인.

    오탐 방지:
    - 2글자 미만 키 제외
    - 키 바로 뒤에 조사, 공백, 구두점, 문장 끝이 와야 함
    """
    if len(key) < _MIN_KEY_LEN:
        return False
    pattern = rf'{re.escape(key)}{_PARTICLE_LOOKAHEAD}'
    return bool(re.search(pattern, text))


# ============================================================================
# 공개 API
# ============================================================================

def extract_sigun_from_history(messages: List[Dict]) -> List[str]:
    """
    전체 대화 히스토리(user 메시지 전체)에서 경상남도 시군을 추출합니다.

    최신 메시지보다 전체 히스토리를 합산하여 검색하므로,
    이전 대화에서 언급된 시군도 기억합니다.

    Returns:
        정규화된 시군 목록 (예: ["경상남도 진주시"])
        없으면 빈 리스트
    """
    # 지연 임포트: 순환 참조 방지
    from app.chat.infra.rag import _extract_sigun_from_message

    user_texts = [
        m.get("content", "")
        for m in messages
        if m.get("role") == "user"
    ]
    if not user_texts:
        return []

    combined = " ".join(user_texts)
    raws = _extract_sigun_from_message(combined)
    normalized = [normalize_sigun(r) for r in raws if r != "경남"]
    return list(dict.fromkeys(s for s in normalized if s.startswith("경상남도 ")))


def check_out_of_scope_region(user_message: str) -> Tuple[bool, str]:
    """
    경상남도 외 지역명이 포함된 경우를 감지합니다.

    Returns:
        (is_out_of_scope, region_display_name)
        - (True, "충남") 처럼 반환; 경남 외 지역 없으면 (False, "")
    """
    for key in _OUT_OF_SCOPE_KEYS_SORTED:
        if key in user_message:
            region = _OUT_OF_SCOPE_REGION_MAP[key]
            logger.info(f"[SigunService] 경상남도 외 지역 감지: '{key}' → '{region}'")
            return True, region
    return False, ""


def extract_ambiguous_location(query: str) -> Tuple[Optional[str], List[str]]:
    """
    쿼리에서 읍/면/동/리명을 찾아 해당하는 시군 후보를 반환합니다.

    조사/경계 조건을 만족하는 첫 번째 키만 반환합니다.

    Returns:
        (location_name, candidate_siguns)
        - 매칭 없으면 (None, [])
        - 매칭 있으면 (location_name, sorted 시군 목록)
    """
    if not _location_dict:
        return None, []

    for key, candidates in _location_dict.items():
        if _match_location_key(key, query):
            logger.info(f"[SigunService] 위치명 '{key}' 감지 → 후보 시군: {candidates}")
            return key, list(candidates)

    return None, []


def count_sigun_ask_attempts(messages: List[Dict]) -> int:
    """
    대화 히스토리에서 시군 되묻기 횟수를 셉니다.

    assistant 메시지에 되묻기 마커가 포함된 경우 카운트합니다.
    """
    count = 0
    for m in messages:
        if m.get("role") != ROLE_ASSISTANT:
            continue
        content = m.get("content", "")
        if _MARKER_NO_REGION in content or _MARKER_AMBIGUOUS in content:
            count += 1
    return count


def build_sigun_ask_message(
    location_name: Optional[str] = None,
    candidates: Optional[List[str]] = None,
) -> str:
    """
    시군 되묻기 메시지를 생성합니다.

    Args:
        location_name: 모호한 위치명 (예: "가산리"). None이면 시군 자체 없음.
        candidates: 후보 시군 목록 (예: ["김해시", "밀양시", ...])

    Returns:
        되묻기 메시지 문자열
    """
    if location_name and candidates and len(candidates) > 1:
        cities = ",".join(f"'{c}'" for c in candidates)
        return f"{cities} {location_name} 중 어디에 해당하시나요?"
    return MSG_ASK_NO_REGION


def extract_eupmyeondong_from_message(user_message: str) -> Optional[str]:
    """
    사용자 메시지에서 읍·면·동명을 추출합니다.

    location_dict 키 중 읍/면/동으로 끝나는 항목을 조사/경계 조건으로 탐색합니다.
    가장 먼저 매칭된 키를 반환합니다.

    Returns:
        읍면동명 문자열 (예: "동읍", "봉림동") 또는 None
    """
    if not _location_dict:
        # fallback: 정규식으로 직접 추출
        m = re.search(r'([가-힣]{2,6}(?:읍|면|동))', user_message)
        return m.group(1) if m else None

    for key in _location_dict:
        if not key.endswith(('읍', '면', '동')):
            continue
        if _match_location_key(key, user_message):
            logger.info(f"[SigunService] 읍면동 감지: '{key}'")
            return key

    # location_dict 미매칭 시 정규식 fallback
    m = re.search(r'([가-힣]{2,6}(?:읍|면|동))', user_message)
    return m.group(1) if m else None


def check_sigun(
    user_message: str,
    messages: List[Dict],
) -> Tuple[List[str], bool, str]:
    """
    시군 추출 및 되묻기 여부를 판단합니다.

    판단 순서:
    1. 전체 히스토리(user 메시지)에서 시군 직접 추출 → 있으면 확정
    2. 현재 메시지에서 읍/면/동/리명 매칭
       - 단일 시군 → 자동 확정
       - 여러 시군 → 되묻기 필요
    3. 아무것도 없음 → 되묻기 필요

    Args:
        user_message: 현재 사용자 메시지
        messages: 전체 대화 히스토리 (현재 user 메시지 포함)

    Returns:
        (sigun_filters, need_clarify, clarify_message)
        - sigun_filters: 확인된 정규화 시군 목록 (예: ["경상남도 진주시"])
        - need_clarify: 되묻기 필요 여부
        - clarify_message: 되묻기 메시지 (need_clarify=True 일 때만 유효)
    """
    logger.info(f"[check_sigun] 호출됨 | query={user_message[:40]!r} | messages 수={len(messages)}")
    # 1. 전체 히스토리에서 시군 추출
    sigun_filters = extract_sigun_from_history(messages)
    logger.info(f"[check_sigun] 히스토리 추출 결과: {sigun_filters}")
    if sigun_filters:
        logger.info(f"[SigunService] 히스토리에서 시군 확인: {sigun_filters}")
        return sigun_filters, False, ""

    # 2. 현재 메시지에서 읍/면/동/리명 매칭
    location_name, candidates = extract_ambiguous_location(user_message)
    if candidates:
        normalized = [normalize_sigun(c) for c in candidates]
        valid = list(dict.fromkeys(s for s in normalized if s.startswith("경상남도 ")))
        if len(valid) == 1:
            # 단일 시군 자동 확정
            logger.info(f"[SigunService] '{location_name}' → 단일 시군 자동 확정: {valid}")
            return valid, False, ""
        elif len(valid) > 1:
            # 여러 시군 → 되묻기
            msg = build_sigun_ask_message(location_name, candidates)
            logger.info(f"[SigunService] '{location_name}' 모호 → 되묻기: {candidates}")
            return [], True, msg

    # 3. 시군 없음 → 되묻기
    logger.info("[SigunService] 시군 미감지 → 지역 되묻기")
    return [], True, MSG_ASK_NO_REGION
