import re
from typing import Dict
from ..base import PatternHandler
from ...config_loader import get_config
#from saltlux.utils.logger import get_logger

class MixedCodeHandler(PatternHandler):
    """
    한글과 인접한 영어(대소문자 불문)를 처리하는 핸들러
    
    목적: 한글 문맥 속에 있는 영어는 쉼표(,) 없이 공백으로만 연결하여
          Local WAV(캐시)가 아닌 Worker(통합 추론)를 타게 함.
          -> 톤이 튀는 현상 방지 및 자연스러운 억양 생성.
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        # self.logger = get_logger("mixed_code_debug")
        
        # 1. 패턴 로드 (두 가지 방향)
        self.pat_kor_eng_str = get_config('patterns.split.mixed_kor_eng', r'([가-힣])(\s*)([a-zA-Z]+(?:[\s]+[a-zA-Z]+)*)')
        self.pat_eng_kor_str = get_config('patterns.split.mixed_eng_kor', r'([a-zA-Z]+(?:[\s]+[a-zA-Z]+)*)(\s*)([가-힣])')
        
        self.regex_kor_eng = re.compile(self.pat_kor_eng_str)
        self.regex_eng_kor = re.compile(self.pat_eng_kor_str)
        
        # 2. 알파벳 발음 매핑 로드 및 정제 (Mixed Code 전용)
        raw_alphabet_dict = get_config('normalization.alphabet', {})
        self.alphabet_dict = self._create_clean_alphabet_map(raw_alphabet_dict)

    def _create_clean_alphabet_map(self, raw_dict: Dict[str, str]) -> Dict[str, str]:
        """
        Mixed Code 문맥에 맞게 알파벳 발음을 정제합니다.
        예: '알파벳 오' -> '오', '쥐' -> '지' (사용자 선호 반영)
        """
        clean_dict = raw_dict.copy()
        
        # [Override] 사용자가 원하는 자연스러운 발음으로 덮어쓰기
        overrides = {
            'o': '오',  # 기존 '알파벳 오' -> '오'
            'e': '이',  # 기존 '알파벳 이' -> '이'
            'z': '제트' # G를 '지'로 쓸 경우 Z와 혼동될 수 있으므로 Z를 명확히 함 (선택사항)
        }
        clean_dict.update(overrides)

        # "알파벳 " 접두어 제거 (안전장치)
        for k, v in clean_dict.items():
            if "알파벳" in v:
                clean_dict[k] = v.replace("알파벳", "").strip()
                
        return clean_dict

    @property
    def name(self) -> str:
        return "mixed_code"

    @property
    def priority(self) -> int:
        # Alphanumeric(60) -> MixedCode(65) -> EnglishSplit(70)
        return 65

    def match(self, text: str) -> bool:
        return bool(self.regex_kor_eng.search(text) or self.regex_eng_kor.search(text))

    def normalize(self, text: str) -> str:
        # [Step 1] (한글 -> 영어) 패턴 처리
        if self.regex_kor_eng.search(text):
            text = self.regex_kor_eng.sub(self._process_kor_eng, text)
            
        # [Step 2] (영어 -> 한글) 패턴 처리
        if self.regex_eng_kor.search(text):
            text = self.regex_eng_kor.sub(self._process_eng_kor, text)
            
        return text

    def _convert_english_to_hangul(self, english_text: str) -> str:
        """영어를 쉼표 없이 공백으로 연결된 한글로 변환 (추론 유도)"""
        converted_list = []
        for char in english_text:
            if char.strip(): # 공백 아님
                key = char.lower()
                # 정제된 맵 사용
                val = self.alphabet_dict.get(key, char)
                converted_list.append(val)
            else:
                # 공백 유지
                converted_list.append(" ")
        return "".join(converted_list)

    def _process_kor_eng(self, match: re.Match) -> str:
        """한글 + 영어 처리 (예: 스 Gol -> 스 지오엘)"""
        kor_part = match.group(1)
        space = match.group(2)
        eng_part = match.group(3)
        
        converted_eng = self._convert_english_to_hangul(eng_part)
        result = f"{kor_part}{space}{converted_eng}"
        
        # 로그에서 변환 결과 확인 가능
        print(f"[Mixed:Kor->Eng] '{match.group(0)}' -> '{result}'")
        return result

    def _process_eng_kor(self, match: re.Match) -> str:
        """영어 + 한글 처리 (예: Kib 행 -> 케이아이비 행)"""
        eng_part = match.group(1)
        space = match.group(2)
        kor_part = match.group(3)
        
        converted_eng = self._convert_english_to_hangul(eng_part)
        result = f"{converted_eng}{space}{kor_part}"
        
        print(f"[Mixed:Eng->Kor] '{match.group(0)}' -> '{result}'")
        return result