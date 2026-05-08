"""
영어 알파벳 분리 핸들러 (English Split)
"""

import re
from ..base import PatternHandler
from ...config_loader import get_config

class EnglishSplitHandler(PatternHandler):
    def __init__(self, config: dict = None):
        self._config = config or {}

        # 1. 패턴 로드 (기본값: 영문자 연속체)
        self.pattern_str = get_config('patterns.split.english', r'[a-zA-Z]+')
        self.pattern = re.compile(self.pattern_str)

        # 2. 매핑 로드 및 안전장치 추가
        # 설정 파일에서 못 가져올 경우를 대비한 영문 알파벳 발음 기본값
        default_alphabet = {
            'a': '에이', 'b': '비', 'c': '씨', 'd': '디', 'e': '이',
            'f': '에프', 'g': '쥐', 'h': '에이치', 'i': '아이', 'j': '제이',
            'k': '케이', 'l': '엘', 'm': '엠', 'n': '엔', 'o': '오',
            'p': '피', 'q': '큐', 'r': '알', 's': '에스', 't': '티',
            'u': '유', 'v': '브이', 'w': '더블유', 'x': '엑스', 'y': '와이', 'z': '제트'
        }

        # get_config 결과가 None이면 default_alphabet 사용
        self.alphabet_dict = get_config('normalization.alphabet') or default_alphabet

    @property
    def name(self) -> str:
        return "english_split"

    @property
    def priority(self) -> int:
        return 70

    def match(self, text: str) -> bool:
        return bool(self.pattern.search(text))

    def normalize(self, text: str) -> str:
        return self.pattern.sub(self._to_chars, text)

    def _to_chars(self, match: re.Match) -> str:
        word = match.group(0)
        converted_list = []
        for char in word:
            key = char.lower()
            # self.alphabet_dict는 이제 무조건 dict이므로 에러가 나지 않습니다.
            val = self.alphabet_dict.get(key, char)
            converted_list.append(val)

        # 결과 예: "JSON" -> ", 제이, 에스, 오, 엔,"
        return ", " + ", ".join(converted_list) + ","