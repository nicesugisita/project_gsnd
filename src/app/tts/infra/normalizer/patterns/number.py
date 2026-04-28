import re
from ..base import PatternHandler


class NumberHandler(PatternHandler):
    """
    숫자 패턴 핸들러 (금액, 퍼센트, 소수점 처리 강화본)
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._load_hardcoded_config()
        self._compile_patterns()

    @property
    def name(self) -> str:
        return "number"

    @property
    def priority(self) -> int:
        return 100

    def _load_hardcoded_config(self) -> None:
        self.native_units = ['개', '명', '살', '시', '마리', '송이', '채', '대', '자루', '통', '켤레', '벌']
        self.sino_units = ['번', '회', '분', '초', '일', '주', '년', '월', '원', '호', '층', '차', '건', '개월']

        self.digit_dict = {
            '0': '공', '1': '일', '2': '이', '3': '삼', '4': '사',
            '5': '오', '6': '육', '7': '칠', '8': '팔', '9': '구'
        }

        self.num_to_kor1 = [""] + list("일이삼사오육칠팔구")
        self.num_to_kor2 = [""] + list("만억조경해")
        self.num_to_kor3 = [""] + list("십백천")

        self.native_ones = ["", "하나", "둘", "셋", "넷", "다섯", "여섯", "일곱", "여덟", "아홉"]
        self.native_tens = ["", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔"]
        self.count_to_kor1 = {1: "한", 2: "두", 3: "세", 4: "네", 20: "스무"}

    def _compile_patterns(self) -> None:
        """정규표현식 우선순위 설정"""
        # 1. 퍼센트 패턴 추가 (%)
        self.percent_pattern = re.compile(r'([+-]?\d+(?:\.\d+)?)\s*(%)')

        # 2. 금액 패턴
        self.currency_pattern = re.compile(r'([+-]?\d{1,3}(?:,\d{3})+|[+-]?\d+)\s*(원)')

        # 3. 소수점 패턴
        self.float_pattern = re.compile(r'(?<!\d)([+-]?\d+)\.(\d{1,5})\d*(?!\d)')

        # 4. 숫자 + 단위
        units_pattern = "|".join(sorted(self.native_units + self.sino_units, key=len, reverse=True))
        self.number_unit_pattern = re.compile(rf'([+-]?\d+)\s*({units_pattern})')

        # 5. 일반 숫자
        self.number_only_pattern = re.compile(r'(?<!\d)([+-]?\d+)(?!\d)')

    def match(self, text: str) -> bool:
        return bool(re.search(r'\d+', text))

    def normalize(self, text: str) -> str:
        # 퍼센트 먼저 처리 (140% -> 백사십 퍼센트)
        text = self.percent_pattern.sub(self._percent_to_korean, text)
        # 금액 처리
        text = self.currency_pattern.sub(self._currency_to_korean, text)
        # 소수점 처리
        text = self.float_pattern.sub(self._float_to_korean, text)
        # 단위 처리
        text = self.number_unit_pattern.sub(self._process_number_and_unit, text)
        # 일반 숫자 처리
        text = self.number_only_pattern.sub(lambda m: self._convert_to_full_korean(m.group(1)), text)
        return text

    def _percent_to_korean(self, match: re.Match) -> str:
        """퍼센트 변환 로직 (정수 및 소수점 포함 퍼센트 대응)"""
        num_str = match.group(1)
        if '.' in num_str:
            # 소수점이 포함된 퍼센트 (예: 10.5%)
            parts = num_str.split('.')
            int_part = self._convert_number_to_sino(int(parts[0].lstrip('+-')))
            float_part = "".join([self.digit_dict.get(d, d) for d in parts[1]])
            prefix = "마이너스 " if num_str.startswith('-') else ""
            return f"{prefix}{int_part} 쩜 {float_part} 퍼센트"
        else:
            # 정수 퍼센트 (예: 140%)
            kor_val = self._convert_number_to_sino(int(num_str.lstrip('+-')))
            prefix = "마이너스 " if num_str.startswith('-') else ""
            return f"{prefix}{kor_val} 퍼센트"

    def _currency_to_korean(self, match: re.Match) -> str:
        num_str = match.group(1).replace(',', '')
        unit = match.group(2)
        prefix = "마이너스 " if num_str.startswith('-') else ""
        n = int(num_str.lstrip('+-'))
        kor_val = self._convert_number_to_sino(n)
        return f"{prefix}{kor_val}{unit}"

    def _convert_number_to_sino(self, n: int) -> str:
        if n == 0: return "영"
        str_val = str(n)
        size = len(str_val)
        result = []
        for i, v_char in enumerate(str_val):
            v = int(v_char)
            position = size - (i + 1)
            if v != 0:
                if not (v == 1 and (position % 4) != 0):
                    result.append(self.num_to_kor1[v])
                result.append(self.num_to_kor3[position % 4])
            if position % 4 == 0 and position > 0:
                section = str_val[max(0, i - 3): i + 1]
                if int(section) > 0:
                    result.append(self.num_to_kor2[position // 4])
        return "".join(result)

    def _float_to_korean(self, match: re.Match) -> str:
        int_part, float_part = match.groups()
        int_kor = self._convert_number_to_sino(int(int_part.lstrip('+-')))
        prefix = "마이너스 " if int_part.startswith('-') else ""
        float_kor = "".join([self.digit_dict.get(d, d) for d in float_part])
        return f"{prefix}{int_kor} 쩜 {float_kor}"

    def _process_number_and_unit(self, match: re.Match) -> str:
        num_str, unit = match.groups()
        n = int(num_str.replace(',', ''))
        if n >= 100 or unit in self.sino_units:
            kor = self._convert_number_to_sino(n)
        else:
            kor = self._convert_number_to_native(n, is_count=True)
        display_unit = "껀" if unit == "건" else unit
        return f"{kor}{display_unit}"

    def _convert_to_full_korean(self, num_str: str) -> str:
        try:
            prefix = "마이너스 " if num_str.startswith('-') else ""
            return prefix + self._convert_number_to_sino(int(num_str.lstrip('+-')))
        except:
            return num_str

    def _convert_number_to_native(self, n: int, is_count: bool = False) -> str:
        if n == 0: return "영"
        if n >= 100: return self._convert_number_to_sino(n)
        ten, one = divmod(n, 10)
        if is_count and n in self.count_to_kor1: return self.count_to_kor1[n]
        kor_ten = self.native_tens[ten]
        kor_one = self.count_to_kor1[one] if (is_count and one in self.count_to_kor1) else self.native_ones[one]
        return kor_ten + kor_one


# 테스트
if __name__ == "__main__":
    handler = NumberHandler()
    print(handler.normalize("349,700원 결제 부탁드립니다."))
    # 출력: 삼십사만구천칠백원 결제 부탁드립니다.
    print(handler.normalize("현재 온도는 -10.5도입니다."))
    # 출력: 현재 온도는 마이너스 십 쩜 오도입니다.