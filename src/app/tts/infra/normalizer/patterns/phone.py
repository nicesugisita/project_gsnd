import re
from typing import Dict
from ..base import PatternHandler


class PhoneHandler(PatternHandler):
    """
    전화번호 패턴 핸들러 (긴급 번호 및 대표 번호 확장 버전)
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._load_hardcoded_config()
        self._compile_patterns()

    def _load_hardcoded_config(self) -> None:
        # 1. 숫자 발음 사전
        self.digit_dict: Dict[str, str] = {
            '0': '공', '1': '일', '2': '이', '3': '삼', '4': '사',
            '5': '오', '6': '육', '7': '칠', '8': '팔', '9': '구',
        }

        self.ten_dict: Dict[str, str] = {
            '1': '십', '2': '이십', '3': '삼십', '4': '사십', '5': '오십'
        }

        # [수정/추가] 패턴 정의
        # 2-1. 긴급 번호 및 짧은 서비스 번호 (112, 119, 1388, 114 등)
        # 앞뒤에 숫자가 붙어있지 않은 독립된 3~4자리 숫자를 타겟팅합니다.
        emergency = r'\b(?:112|119|114|120|1339|1388|1544|1588)\b'

        # 2-2. 기존 일반 전화번호 패턴
        mobile = r'(?:\+?82[-.\s]?)?0(?:10|11|16|17|18|19)[-.]?\d{3,4}[-.]?\d{4}'
        area = r'(?:\+?82[-.\s]?)?0(?:2|[3-6]\d)[-.]?\d{3,4}[-.]?\d{4}'
        service = r'(?:15|16|18)\d{2}[-.]?\d{4}'
        short_area = r'0(?:2|[3-6]\d)[-.]\d{3}'

        # 모든 단일 번호 패턴 통합 (emergency를 가장 앞에 두어 우선 매칭 유도)
        self.single_phone_str = f"(?:{emergency}|{mobile}|{area}|{service}|{short_area})"

        # 3. 범위형 패턴
        self.range_pattern_str = rf'({self.single_phone_str})\s?~\s?(\d{{1,4}})'

        # 4. 추가 뒷자리 패턴
        self.extra_tail_pattern_str = r'[\s,]+(\d{{4}})(?=\D|$)'

    def _compile_patterns(self) -> None:
        # \b(경계)나 공백 조건을 주어 일반 숫자와 섞이지 않게 합니다.
        self.range_pattern = re.compile(rf'(?:(?<=\s)|^){self.range_pattern_str}(?=\D|$)')
        self.phone_pattern = re.compile(rf'(?:(?<=\s)|^){self.single_phone_str}(?=\D|$)')
        self.extra_tail_pattern = re.compile(self.extra_tail_pattern_str)

    @property
    def name(self) -> str:
        return "phone"

    @property
    def priority(self) -> int:
        # [핵심] 일반 숫자 핸들러(보통 20~30)보다 높은 우선순위를 부여합니다.
        return 10

    def match(self, text: str) -> bool:
        return bool(self.range_pattern.search(text) or self.phone_pattern.search(text))

    def normalize(self, text: str) -> str:
        text = self.range_pattern.sub(self._range_to_korean, text)
        text = self.phone_pattern.sub(self._phone_to_korean, text)
        text = self._normalize_extra_tails_with_connector(text)
        return text

    def _digit_to_korean(self, digit: str) -> str:
        return self.digit_dict.get(digit, digit)

    def _phone_to_korean(self, match: re.Match) -> str:
        return self._convert_sequence(match.group())

    def _range_to_korean(self, match: re.Match) -> str:
        start_phone = match.group(1)
        end_digits = match.group(2)
        start_korean = self._convert_sequence(start_phone).strip().rstrip(',')

        if len(end_digits) == 1:
            end_korean = self._digit_to_korean(end_digits)
        elif len(end_digits) == 2:
            ten = self.ten_dict.get(end_digits[0], "")
            one = self.digit_dict.get(end_digits[1], "")
            if one == "공": one = ""
            end_korean = f"{ten}{one}"
        else:
            end_korean = "".join([self._digit_to_korean(d) for d in end_digits])

        return f"{start_korean} 에서 {end_korean}"

    def _convert_sequence(self, seq_str: str) -> str:
        result = []
        for ch in seq_str:
            if ch.isdigit():
                result.append(self._digit_to_korean(ch))
            elif ch in ['-', '.']:
                if result and result[-1] != ", ":
                    result.append(", ")
        return "".join(result)

    def _normalize_extra_tails_with_connector(self, text: str) -> str:
        def replace_tail(match):
            digits = match.group(1)
            converted = [self._digit_to_korean(d) for d in digits]
            return " 그리고 " + ", ".join(converted) + ","

        return self.extra_tail_pattern.sub(replace_tail, text)