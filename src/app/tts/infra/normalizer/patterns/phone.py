import re
from typing import Dict
from ..base import PatternHandler


class PhoneHandler(PatternHandler):
    """
    전화번호 패턴 핸들러 (범위 패턴 추가 버전)

    특징:
    1. 055-330-6661~6664 같은 범위형 패턴 인식 (~부터 ~로 변환)
    2. 하이픈 없는 연속 숫자(0553304554) 및 대표 번호(1588) 인식
    3. 하이픈(-)을 '다시'로 변환
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._load_hardcoded_config()
        self._compile_patterns()

    def _load_hardcoded_config(self) -> None:
        """코드 내부에 직접 발음 및 정규식 패턴 정의"""
        # 1. 숫자 발음 사전
        self.digit_dict: Dict[str, str] = {
            '0': '공', '1': '일', '2': '이', '3': '삼', '4': '사',
            '5': '오', '6': '육', '7': '칠', '8': '팔', '9': '구',
        }

        # 2. 개별 전화번호 기본 패턴들
        mobile = r'(?:\+?82[-.\s]?)?0(?:10|11|16|17|18|19)[-.]?\d{3,4}[-.]?\d{4}'
        area = r'(?:\+?82[-.\s]?)?0(?:2|[3-6]\d)[-.]?\d{3,4}[-.]?\d{4}'
        service = r'(?:15|16|18)\d{2}[-.]?\d{4}'
        short_area = r'0(?:2|[3-6]\d)[-.]\d{3}'

        # 모든 단일 번호 패턴 통합
        self.single_phone_str = f"(?:{mobile}|{area}|{service}|{short_area})"

        # 3. [신규] 범위형 패턴 (예: 055-330-6661~6664)
        # 단일 번호 패턴 뒤에 '~'와 숫자(1~4자리)가 오는 경우
        self.range_pattern_str = rf'({self.single_phone_str})\s?~\s?(\d{{1,4}})'

        # 4. 추가 뒷자리 패턴 (기존 유지)
        self.extra_tail_pattern_str = r'[\s,]+(\d{{4}})(?=\D|$)'

    def _compile_patterns(self) -> None:
        """정규표현식 컴파일"""
        # 범위 패턴을 단일 패턴보다 먼저 매칭해야 함
        self.range_pattern = re.compile(rf'(?:(?<=\s)|^){self.range_pattern_str}(?=\D|$)')
        self.phone_pattern = re.compile(rf'(?:(?<=\s)|^){self.single_phone_str}(?=\D|$)')
        self.extra_tail_pattern = re.compile(self.extra_tail_pattern_str)

    @property
    def name(self) -> str:
        return "phone"

    @property
    def priority(self) -> int:
        return 10

    def match(self, text: str) -> bool:
        return bool(self.range_pattern.search(text) or self.phone_pattern.search(text))

    def normalize(self, text: str) -> str:
        # 1단계: 범위형 패턴 처리 (055-330-6661~6664 -> ...육육육일 부터 육육육사 로)
        text = self.range_pattern.sub(self._range_to_korean, text)

        # 2단계: 남은 단일 번호 변환
        text = self.phone_pattern.sub(self._phone_to_korean, text)

        # 3단계: 뒤따르는 숫자 4자리 '그리고' 연결
        text = self._normalize_extra_tails_with_connector(text)

        return text

    def _digit_to_korean(self, digit: str) -> str:
        return self.digit_dict.get(digit, digit)

    def _phone_to_korean(self, match: re.Match) -> str:
        """단일 번호를 낱글자로 읽고 구분 기호를 '다시'로 처리"""
        phone_str = match.group()
        return self._convert_sequence(phone_str)

    def _range_to_korean(self, match: re.Match) -> str:
        """범위형 패턴(~표시)을 '부터 ~로' 형태로 변환"""
        start_phone = match.group(1)  # 055-330-6661
        end_digits = match.group(2)  # 6664

        # 시작 번호 변환 (마지막 쉼표 제거)
        start_korean = self._convert_sequence(start_phone).rstrip(',')

        # 끝 번호(숫자만) 변환
        end_korean = "".join([self._digit_to_korean(d) for d in end_digits])

        return f"{start_korean} 부터 {end_korean} 로"

    def _convert_sequence(self, seq_str: str) -> str:
        """
        숫자는 붙여서 변환하고,
        하이픈(-)이나 마침표(.)가 있던 자리만 쉼표(,)로 치환
        """
        result = []
        for ch in seq_str:
            if ch.isdigit():
                # 숫자는 변환해서 바로 넣기 (공백 없이 붙음)
                result.append(self._digit_to_korean(ch))
            elif ch in ['-', '.']:
                # 하이픈이나 점을 만나면 쉼표와 공백을 추가하여 끊어 읽기 유도
                # 마지막 요소가 이미 쉼표라면 중복 추가 방지
                if result and result[-1] != ", ":
                    result.append(", ")

        return "".join(result)

    # def _convert_sequence(self, seq_str: str) -> str:
    #     """숫자와 하이픈이 섞인 문자열을 한글 발음으로 변환하는 공통 로직"""
    #     result = []
    #     for ch in seq_str:
    #         if ch.isdigit():
    #             result.append(self._digit_to_korean(ch))
    #         elif ch in ['-', '.']:
    #             result.append("다시")
    #     return ", ".join(result) + ","

    def _normalize_extra_tails_with_connector(self, text: str) -> str:
        def replace_tail(match):
            digits = match.group(1)
            converted = [self._digit_to_korean(d) for d in digits]
            return " 그리고 " + ", ".join(converted) + ","

        return self.extra_tail_pattern.sub(replace_tail, text)