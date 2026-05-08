# /data/user/uyeong/TTS/TTS_vits/master/saltlux/preprocessing/normalizer/patterns/alphanumeric.py
"""
영문+숫자 혼합 핸들러 (Alphanumeric Handler)

ID, 비밀번호, 모델명 등 영어와 숫자가 붙어있는 경우(예: hyundaicard112212, Json1312Bourn),
숫자를 카디널(천백이십...)이 아닌 디짓(일, 일, 이, 이...)으로 읽도록 변환합니다.
"""

import re
from typing import Dict
from ..base import PatternHandler
from ...config_loader import get_config

class AlphanumericHandler(PatternHandler):
    def __init__(self, config: dict = None):
        self._config = config or {}
        
        # 1. 패턴 로드 (patterns.yaml)
        # 예: [a-zA-Z]+\d+... (영어+숫자 조합)
        self.pattern_str = get_config(
            'patterns.split.alphanumeric', 
            r'(?<![가-힣])(?:[a-zA-Z]+\d+|\d+[a-zA-Z]+)[a-zA-Z0-9]*'
        )
        self.pattern = re.compile(self.pattern_str)
        
        # 2. 숫자 발음 매핑 (normalization.yaml -> digits)
        # 'normalization.phone_digits' 또는 'normalization.digits' 사용
        self.digit_dict: Dict[str, str] = get_config(
            'normalization.phone_digits',
            {'0': '공', '1': '일', '2': '이', '3': '삼', '4': '사', 
             '5': '오', '6': '육', '7': '칠', '8': '팔', '9': '구'}
        )
        
        # 내부에서 숫자만 찾기 위한 정규식
        self.digit_finder = re.compile(r'\d+')

    @property
    def name(self) -> str:
        return "alphanumeric"

    @property
    def priority(self) -> int:
        # EnglishSplit(70)과 Number(100)보다 먼저 실행되어야 함
        # DigitSplit(50) 보다는 뒤에 실행 (순수 숫자열 우선 처리 후 남은 섞인 문자열 처리)
        return 60

    def match(self, text: str) -> bool:
        return bool(self.pattern.search(text))

    def normalize(self, text: str) -> str:
        return self.pattern.sub(self._process_match, text)

    def _process_match(self, match: re.Match) -> str:
        word = match.group(0)
        
        # 매칭된 단어(예: hyundaicard112212) 내부의 "숫자 덩어리"만 찾아서 변환
        # 예: "112212" -> "일, 일, 이, 이, 일, 이,"
        def replace_digits(m):
            digits = m.group(0)
            converted = []
            for d in digits:
                converted.append(self.digit_dict.get(d, d))
            
            # 숫자 사이와 끝에 콤마를 넣어 Local WAV 유도 (및 영어와 분리)
            # 앞뒤에 공백을 넣어 영어와 확실히 떼어놓음
            return " " + ", ".join(converted) + ", "

        # 단어 내의 숫자를 모두 디짓 발음으로 치환
        # hyundaicard112212 -> hyundaicard 일, 일, 이, 이, 일, 이, 
        processed_word = self.digit_finder.sub(replace_digits, word)
        
        return processed_word