import re
from typing import Dict
from ..base import PatternHandler

import re
from typing import Dict


class PhoneHandler(PatternHandler):
    """
    전화번호 패턴 핸들러 (낱글자 공백 분리 및 070 패턴 추가 버전)
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._load_hardcoded_config()
        self._compile_patterns()

    def _load_hardcoded_config(self) -> None:
        """코드 내부에 직접 발음 및 정규식 패턴 정의"""
        # 1. 숫자 발음 사전 (0 -> 공, 1 -> 일 ...)
        self.digit_dict: Dict[str, str] = {
            '0': '공', '1': '일', '2': '이', '3': '삼', '4': '사',
            '5': '오', '6': '육', '7': '칠', '8': '팔', '9': '구',
        }

        self.ten_dict: Dict[str, str] = {
            '1': '십', '2': '이십', '3': '삼십', '4': '사십', '5': '오십'
        }

        # 2. NumberHandler 간섭 방지를 위한 제외 단위 설정
        units = ['원', '%', '퍼센트', '개', '명', '살', '시', '마리', '분', '초', '도', '층', '호']
        self.exclude_lookahead = f"(?!\\s*({'|'.join(units)}))"

        # 3. 개별 전화번호 패턴 정의 (070 및 인터넷전화 계열 추가)
        emergency = r'\b(?:112|119|114|120|1339|1388|1544|1588|129)\b'
        mobile = r'(?:\+?82[-.\s]?)?0(?:10|11|16|17|18|19)[-.]?\d{3,4}[-.]?\d{4}'
        # [수정] 지역번호 매칭 영역에 70(인터넷전화)을 추가하였습니다.
        area = r'(?:\+?82[-.\s]?)?0(?:2|[3-6]\d|70)[-.]?\d{3,4}[-.]?\d{4}'
        service = r'(?:15|16|18)\d{2}[-.]?\d{4}'
        short_area = r'0(?:2|[3-6]\d|70)[-.]\d{3}'

        # 모든 단일 번호 패턴 통합
        self.single_phone_str = f"(?:{emergency}|{mobile}|{area}|{service}|{short_area})"

        # 4. 범위형 패턴 (055-225-7208~14 등)
        self.range_pattern_str = rf'({self.single_phone_str})\s?~\s?(\d{{1,4}}){self.exclude_lookahead}'

        # 5. 최종 단일 번호 패턴 (단위 제외 조건 포함, 단어 경계 \b 활용)
        self.final_phone_pattern_str = rf'\b{self.single_phone_str}{self.exclude_lookahead}'

        # 6. 추가 뒷자리 패턴 (기존 유지)
        self.extra_tail_pattern_str = r'[\s,]+(\d{{4}})(?=\D|$)'

    def _compile_patterns(self) -> None:
        """정규표현식 컴파일"""
        self.range_pattern = re.compile(self.range_pattern_str)
        self.phone_pattern = re.compile(self.final_phone_pattern_str)
        self.extra_tail_pattern = re.compile(self.extra_tail_pattern_str)

    @property
    def name(self) -> str:
        return "phone"

    @property
    def priority(self) -> int:
        # NumberHandler(100)보다 먼저 실행되도록 20으로 설정
        return 20

    def match(self, text: str) -> bool:
        return bool(self.range_pattern.search(text) or self.phone_pattern.search(text))

    def normalize(self, text: str) -> str:
        # 1단계: 범위형 패턴 처리
        text = self.range_pattern.sub(self._range_to_korean, text)

        # 2단계: 남은 단일 번호 변환 (공칠공 팔공구팔 사육이육 구조 적용)
        text = self.phone_pattern.sub(self._phone_to_korean, text)

        # 3단계: 뒤따르는 숫자 4자리 '그리고' 연결
        text = self._normalize_extra_tails_with_connector(text)

        return text

    def _digit_to_korean(self, digit: str) -> str:
        return self.digit_dict.get(digit, digit)

    def _phone_to_korean(self, match: re.Match) -> str:
        """번호를 낱글자로 변환"""
        return self._convert_sequence(match.group())

    def _range_to_korean(self, match: re.Match) -> str:
        """범위형 패턴(~표시)을 '에서' 형태로 변환"""
        start_phone = match.group(1)
        end_digits = match.group(2)

        start_korean = self._convert_sequence(start_phone).strip()

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
        """[수정] 숫자는 낱글자로 붙여 읽고, 하이픈(-)이나 마침표(.) 기호는 '공백'으로 변환합니다."""
        result = []
        for ch in seq_str:
            if ch.isdigit():
                result.append(self._digit_to_korean(ch))
            elif ch in ['-', '.']:
                # 이전 글자가 공백이 아닐 때만 공백 한 칸 추가 (중복 방지)
                if result and result[-1] != " ":
                    result.append(" ")
        return "".join(result)

    def _normalize_extra_tails_with_connector(self, text: str) -> str:
        def replace_tail(match):
            digits = match.group(1)
            converted = [self._digit_to_korean(d) for d in digits]
            return " 그리고 " + "".join(converted)

        return self.extra_tail_pattern.sub(replace_tail, text)