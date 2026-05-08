"""
공백/콤마 숫자 분리 핸들러 (Digit Split)
"""
import re
from ..base import PatternHandler
from ...config_loader import get_config

class DigitSplitHandler(PatternHandler):
    def __init__(self, config: dict = None):
        self._config = config or {}
        
        # 1. 패턴 로드 (patterns.yaml에서 수정된 정규식 사용)
        self.pattern_str = get_config(
            'patterns.split.digit', 
            r'(?<!\d)(\d+(?:[\s,]+\d+)+)(?!\d)'
        )
        self.pattern = re.compile(self.pattern_str)
        
        # 2. 숫자 매핑 로드 및 안전장치 추가
        # 설정 파일에 'normalization.phone_digits'가 없거나 None일 경우 사용할 기본값
        default_digits = {
            '0': '영', '1': '일', '2': '이', '3': '삼', '4': '사',
            '5': '오', '6': '육', '7': '칠', '8': '팔', '9': '구'
        }

        # get_config 결과가 None이면 default_digits를 할당
        self.digit_dict = get_config('normalization.phone_digits') or default_digits

    @property
    def name(self) -> str:
        return "digit_split"

    @property
    def priority(self) -> int:
        return 50

    def match(self, text: str) -> bool:
        return bool(self.pattern.search(text))

    def normalize(self, text: str) -> str:
        return self.pattern.sub(self._process_match, text)

    def _process_match(self, match: re.Match) -> str:
        content = match.group(1)

        # 공백(\s)과 콤마(,)를 기준으로 숫자 뭉치 분리
        tokens = re.split(r'[\s,]+', content)

        converted_tokens = []
        for token in tokens:
            if not token:
                continue

            # 한글 변환 (1 -> 일)
            # self.digit_dict가 이제 무조건 딕셔너리임을 보장하므로 .get() 에러가 나지 않습니다.
            korean_token = ''.join(self.digit_dict.get(ch, ch) for ch in token)
            converted_tokens.append(korean_token)

        # 콤마와 공백으로 다시 연결 (결과 예: ", 일, 이, 이,")
        return ", " + ", ".join(converted_tokens) + ","