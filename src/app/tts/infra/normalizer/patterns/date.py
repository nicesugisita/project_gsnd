import re
from typing import Dict
from ..base import PatternHandler

class DateHandler(PatternHandler):
    """
    날짜 패턴 핸들러
    - 6월 -> 유월, 10월 -> 시월 예외 처리 완료
    - 범위형 날짜 (~부터 ~로) 처리
    - 2자리 연도 보정 (26년 -> 2026년)
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._load_hardcoded_config()
        self._compile_patterns()

    def _load_hardcoded_config(self) -> None:
        self.digit_dict: Dict[str, str] = {
            '0': '공', '1': '일', '2': '이', '3': '삼', '4': '사',
            '5': '오', '6': '육', '7': '칠', '8': '팔', '9': '구',
        }

        # 정규식 패턴 정의
        single_date_part = r'(\d{2,4})[-./](\d{1,2})[-./](\d{1,2})\.?'
        self.date_range_str = rf'(?<!\d){single_date_part}\s?~\s?{single_date_part}(?!\d)'
        self.date_checker_str = r'(?<!\d)(\d{2,4})[-./](\d{1,2})[-./](\d{1,2})\.?(?!\d)'
        self.date_korean_full_str = r'(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일'
        self.date_korean_ym_str = r'(\d{4})년\s*(\d{1,2})월'
        self.date_korean_md_str = r'(\d{1,2})월\s*(\d{1,2})일'
        self.compact_date_digits_str = r'(?<!\d)(20\d{6})(?!\d)'

    def _compile_patterns(self) -> None:
        self.date_range = re.compile(self.date_range_str)
        self.date_checker = re.compile(self.date_checker_str)
        self.date_korean_full = re.compile(self.date_korean_full_str)
        self.date_korean_ym = re.compile(self.date_korean_ym_str)
        self.date_korean_md = re.compile(self.date_korean_md_str)
        self.compact_date_digits = re.compile(self.compact_date_digits_str)

    @property
    def name(self) -> str:
        return "date"

    @property
    def priority(self) -> int:
        return 5

    def match(self, text: str) -> bool:
        return bool(self.date_range.search(text) or self.date_checker.search(text))

    def normalize(self, text: str) -> str:
        # 1. 범위형 (26.1.1 ~ 26.10.31)
        text = self.date_range.sub(self._date_range_to_korean, text)
        # 2. 단일 날짜 (2026.06.15)
        text = self.date_checker.sub(self._date_to_korean, text)
        # 3. 한글 결합형 날짜
        text = self.date_korean_full.sub(self._process_korean_date_full, text)
        text = self.date_korean_ym.sub(self._process_korean_date_ym, text)
        text = self.date_korean_md.sub(self._process_korean_date_md, text)
        # 4. 8자리 숫자 (20260417)
        text = self.compact_date_digits.sub(lambda m: self._digits_to_korean(m.group(1)), text)
        return text

    def _fix_year(self, year_str: str) -> int:
        year = int(year_str)
        return 2000 + year if len(year_str) == 2 else year

    def _get_month_kor(self, m) -> str:
        """6월->유, 10월->시 예외처리 포함 월 발음 변환"""
        try:
            val = int(m)
            if val == 6: return "유"
            if val == 10: return "시"
            return self._number_to_sino(val)
        except:
            return str(m)

    def _date_range_to_korean(self, match: re.Match) -> str:
        y1, m1, d1, y2, m2, d2 = match.groups()
        start = f"{self._number_to_sino(self._fix_year(y1))}년 {self._get_month_kor(m1)}월 {self._number_to_sino(int(d1))}일"
        end = f"{self._number_to_sino(self._fix_year(y2))}년 {self._get_month_kor(m2)}월 {self._number_to_sino(int(d2))}일"
        return f"{start} 부터 {end} 로"

    def _date_to_korean(self, match: re.Match) -> str:
        y, m, d = match.groups()
        return f"{self._number_to_sino(self._fix_year(y))}년 {self._get_month_kor(m)}월 {self._number_to_sino(int(d))}일"

    def _process_korean_date_full(self, match: re.Match) -> str:
        y, m, d = match.groups()
        return f"{self._number_to_sino(int(y))}년 {self._get_month_kor(m)}월 {self._number_to_sino(int(d))}일"

    def _process_korean_date_ym(self, match: re.Match) -> str:
        y, m = match.groups()
        return f"{self._number_to_sino(int(y))}년 {self._get_month_kor(m)}월"

    def _process_korean_date_md(self, match: re.Match) -> str:
        m, d = match.groups()
        return f"{self._get_month_kor(m)}월 {self._number_to_sino(int(d))}일"

    def _digits_to_korean(self, digits: str) -> str:
        return ''.join(self.digit_dict.get(d, d) for d in digits)

    def _number_to_sino(self, n: int) -> str:
        if n == 0: return "영"
        num_to_kor1, num_to_kor2, num_to_kor3 = [""] + list("일이삼사오육칠팔구"), [""] + list("만억조경해"), [""] + list("십백천")
        str_val = str(n)
        size, kor_list = len(str_val), []
        for i, v_char in enumerate(str_val):
            v, pos = int(v_char), size - (i + 1)
            if v != 0:
                if not (v == 1 and pos % 4 != 0): kor_list.append(num_to_kor1[v])
                kor_list.append(num_to_kor3[pos % 4])
            if pos % 4 == 0 and pos > 0:
                if int(str_val[max(0, i - 3):i + 1]) > 0: kor_list.append(num_to_kor2[pos // 4])
        return "".join(kor_list)