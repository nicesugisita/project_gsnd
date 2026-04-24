"""
연도 필터 추출 유틸리티

사용자 메시지에서 연도 패턴을 감지하여 4자리 연도 리스트로 반환합니다.
- 4자리: "2026년", "2025년도" → ["2026", "2025"]
- 2자리: "26년", "25년도" → ["2026", "2025"] (앞에 "20" 붙임)
- 상대 연도: "올해" → 현재 연도, "작년" → 현재-1, "재작년" → 현재-2
"""

import logging
import re
from datetime import date
from typing import List

logger = logging.getLogger(__name__)

# 4자리 연도: 2026년, 2025년도
_PATTERN_4DIGIT = re.compile(r'(\d{4})\s*(?:년도?)')

# 2자리 연도: 26년, 25년도 (4자리에 포함되지 않는 위치만)
# negative lookbehind로 앞에 숫자가 없는 경우만 매칭
_PATTERN_2DIGIT = re.compile(r'(?<!\d)(\d{2})\s*(?:년도?)')

# N년 전/후: "3년 전" → current_year - 3, "2년 후" → current_year + 2
_PATTERN_N_YEARS_AGO = re.compile(r'(\d{1,2})\s*년\s*(전|후)')

# 상대 연도: 재작년 → -2, 작년 → -1, 올해/금년 → 0, 내년 → +1
_RELATIVE_YEAR_MAP = {
    "재작년": -2,
    "작년": -1,
    "올해": 0,
    "금년": 0,
    "내년": 1,
}
# "재작년"을 "작년"보다 먼저 매칭해야 하므로 긴 키워드 우선 정렬
_PATTERN_RELATIVE = re.compile(
    r'(' + '|'.join(sorted(_RELATIVE_YEAR_MAP.keys(), key=len, reverse=True)) + r')'
)


def extract_year_filters(message: str) -> List[str]:
    """사용자 메시지에서 연도 패턴을 추출하여 4자리 연도 리스트로 반환.

    Args:
        message: 사용자 메시지

    Returns:
        4자리 연도 문자열 리스트 (중복 제거, 순서 유지).
        연도가 없으면 빈 리스트.

    Examples:
        >>> extract_year_filters("2026년 복지사업")
        ["2026"]
        >>> extract_year_filters("25년도랑 2026년도 비교")
        ["2025", "2026"]
        >>> extract_year_filters("복지사업 알려줘")
        ["2026"]  # 연도 미감지 시 현재 연도 반환
    """
    if not message:
        return []

    current_year = date.today().year
    seen = set()
    result: List[str] = []

    # 0단계: 상대 연도 추출 (올해, 작년, 재작년, 내년, 금년)
    for match in _PATTERN_RELATIVE.finditer(message):
        keyword = match.group(1)
        offset = _RELATIVE_YEAR_MAP[keyword]
        year = str(current_year + offset)
        if year not in seen:
            seen.add(year)
            result.append(year)
            logger.debug(f"[YearFilter] 상대 연도 매칭: '{keyword}' → {year}")

    # 0.5단계: N년 전/후 추출 ("3년 전" → current_year - 3, "2년 후" → current_year + 2)
    for match in _PATTERN_N_YEARS_AGO.finditer(message):
        n = int(match.group(1))
        direction = match.group(2)
        offset = -n if direction == "전" else n
        year = str(current_year + offset)
        if year not in seen:
            seen.add(year)
            result.append(year)
            logger.debug(f"[YearFilter] N년 전/후 매칭: '{match.group(0)}' → {year}")

    # 1단계: 4자리 연도 추출
    for match in _PATTERN_4DIGIT.finditer(message):
        year = match.group(1)
        if year not in seen:
            seen.add(year)
            result.append(year)

    # 2단계: 2자리 연도 추출 (4자리/"N년 전/후" 매칭 위치와 겹치지 않도록)
    four_digit_spans = {(m.start(), m.end()) for m in _PATTERN_4DIGIT.finditer(message)}
    n_years_spans = {(m.start(), m.end()) for m in _PATTERN_N_YEARS_AGO.finditer(message)}
    exclude_spans = four_digit_spans | n_years_spans

    for match in _PATTERN_2DIGIT.finditer(message):
        # 이 2자리 매칭이 다른 패턴(4자리, N년 전/후)의 일부인지 확인
        is_part_of_other = any(
            span_start <= match.start() < span_end
            for span_start, span_end in exclude_spans
        )
        if is_part_of_other:
            continue

        short_year = match.group(1)
        full_year = f"20{short_year}"
        if full_year not in seen:
            seen.add(full_year)
            result.append(full_year)

    if result:
        logger.info(f"[YearFilter] 메시지에서 연도 추출: {result}")
    else:
        logger.debug(f"[YearFilter] 메시지에서 연도 미감지 : {current_year}")
        result = [str(current_year)]

    return result
