import re
from typing import Dict, List
from ..base import PatternHandler



class NumberHandler(PatternHandler):
    """
    숫자 패턴 핸들러 (고유어 십 단위 및 소수점 처리 수정본)
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._load_hardcoded_config()
        self._compile_patterns()

    def _load_hardcoded_config(self) -> None:
        """하드코딩 설정"""
        self.native_units = ['개', '명', '살', '시', '마리', '송이', '채', '대', '자루', '통', '켤레', '벌']
        self.sino_units = ['번', '회', '분', '초', '일', '주', '년', '월', '원', '호', '층', '차', '건', '개월']

        self.digit_dict = {
            '0': '공', '1': '일', '2': '이', '3': '삼', '4': '사',
            '5': '오', '6': '육', '7': '칠', '8': '팔', '9': '구'
        }

        self.num_to_kor1 = [""] + list("일이삼사오육칠팔구")
        self.num_to_kor2 = [""] + list("만억조경해")
        self.num_to_kor3 = [""] + list("십백천")

        # 고유어 수사 매핑 (100 미만용)
        self.native_ones = ["", "하나", "둘", "셋", "넷", "다섯", "여섯", "일곱", "여덟", "아홉"]
        self.native_tens = ["", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔"]

        # 단위와 결합할 때 변하는 고유어 (1, 2, 3, 4, 20)
        self.count_to_kor1 = {1: "한", 2: "두", 3: "세", 4: "네", 20: "스무"}

    def _compile_patterns(self) -> None:
        """정규표현식 컴파일"""
        units_pattern = "|".join(sorted(self.native_units + self.sino_units, key=len, reverse=True))

        self.currency_pattern = re.compile(r'(?<!\d)([+-]?\d{1,3}(?:,\d{3})+)(?:\s*(원))?(?!\d)')
        self.float_pattern = re.compile(r'(?<!\d)([+-]?\d+)\.(\d{1,5})\d*(?!\d)')
        self.number_unit_pattern = re.compile(rf'([+-]?\d+)\s*({units_pattern})')
        self.number_only_pattern = re.compile(r'(?<!\d)([+-]?\d+)(?!\d)')

    @property
    def name(self) -> str:
        return "number"

    @property
    def priority(self) -> int:
        return 100

    def match(self, text: str) -> bool:
        return bool(re.search(r'\d+', text))

    def normalize(self, text: str) -> str:
        text = self.currency_pattern.sub(self._currency_to_korean, text)
        text = self.float_pattern.sub(self._float_to_korean, text)
        text = self.number_unit_pattern.sub(lambda m: self._process_number_and_unit(m), text)
        text = self.number_only_pattern.sub(lambda m: self._convert_to_full_korean(m.group(1)), text)
        return text

    def _float_to_korean(self, match: re.Match) -> str:
        int_part, float_part = match.groups()
        int_kor = self._convert_to_full_korean(int_part)
        float_kor = "".join([self.digit_dict.get(d, d) for d in float_part])
        return f"{int_kor} 쩜 {float_kor}"

    def _currency_to_korean(self, match: re.Match) -> str:
        num_str = match.group(1).replace(',', '').lstrip('+-')
        prefix = "마이너스 " if match.group(1).startswith('-') else ""
        kor_val = self._convert_number_to_sino(int(num_str))
        return f"{prefix}{kor_val}{match.group(2) or ''}"

    def _process_number_and_unit(self, match: re.Match) -> str:
        num_str, unit = match.groups()
        n = int(num_str.replace(',', ''))

        # 100 이상이거나 한자어 단위인 경우
        if n >= 100 or unit in self.sino_units:
            kor = self._convert_number_to_sino(n)
        else:
            # 고유어 변환 (100 미만 + 고유어 단위)
            kor = self._convert_number_to_native(n, is_count=True)

        display_unit = "껀" if unit == "건" else unit
        return f"{kor}{display_unit}"

    def _convert_to_full_korean(self, num_str: str) -> str:
        try:
            prefix = "마이너스 " if num_str.startswith('-') else ""
            return prefix + self._convert_number_to_sino(int(num_str.lstrip('+-')))
        except:
            return num_str

    def _convert_number_to_sino(self, n: int) -> str:
        if n == 0: return "영"
        str_val, kor_list = str(n), []
        size = len(str_val)
        for i, v_char in enumerate(str_val):
            v, position = int(v_char), size - (i + 1)
            if v != 0:
                if not (v == 1 and position % 4 != 0):
                    kor_list.append(self.num_to_kor1[v])
                kor_list.append(self.num_to_kor3[position % 4])
            if position % 4 == 0 and position > 0:
                if int(str_val[max(0, i - 3):i + 1]) > 0:
                    kor_list.append(self.num_to_kor2[position // 4])
        return "".join(kor_list)

    def _convert_number_to_native(self, n: int, is_count: bool = False) -> str:
        """
        n < 100 고유어 변환
        is_count=True일 때 1, 2, 3, 4, 20을 '한, 두, 세, 네, 스무'로 변환
        """
        if n == 0: return "영"
        if n >= 100: return self._convert_number_to_sino(n)

        ten, one = divmod(n, 10)

        # 1. 십 단위와 일 단위 결합
        if is_count and n in self.count_to_kor1:
            return self.count_to_kor1[n]

        kor_ten = self.native_tens[ten]

        if is_count and one in self.count_to_kor1:
            kor_one = self.count_to_kor1[one]
        else:
            kor_one = self.native_ones[one]

        return kor_ten + kor_one