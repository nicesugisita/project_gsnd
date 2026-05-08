"""
한글 숫자 분리 핸들러 (Korean Digit Split)

예: "공 일 이 삼" -> "공, 일, 이, 삼,"
이렇게 변환해야 TokenManager가 각각을 'digit' 토큰으로 인식하여 Local WAV를 사용합니다.
"""
import re
from ..base import PatternHandler

class KoreanDigitSplitHandler(PatternHandler):
    def __init__(self, config: dict = None):
        self._config = config or {}
        
        # 한글 숫자 문자셋 정의
        self.kor_digits = "영공일이삼사오육칠팔구"
        
        # 정규식 패턴:
        # 1. (?<![가-힣]): 앞에 다른 한글이 없어야 함 (단어 중간 매칭 방지)
        # 2. [영공...구]: 한글 숫자로 시작
        # 3. (?:[\s,]+[영공...구]+)+: 공백/콤마 + 한글 숫자가 1회 이상 반복
        # 4. (?![가-힣]): 뒤에 다른 한글이 없어야 함
        self.pattern = re.compile(
            r'(?<![가-힣])([' + self.kor_digits + r'](?:[\s,]+[' + self.kor_digits + r']+)+)(?![가-힣])'
        )

    @property
    def name(self) -> str:
        return "korean_digit_split"

    @property
    def priority(self) -> int:
        return 55 # DigitSplit(50) 다음에 실행

    def match(self, text: str) -> bool:
        return bool(self.pattern.search(text))

    def normalize(self, text: str) -> str:
        return self.pattern.sub(self._process_match, text)

    def _process_match(self, match: re.Match) -> str:
        content = match.group(1)
        
        # 공백이나 콤마로 쪼갬
        tokens = re.split(r'[\s,]+', content)
        
        # 빈 토큰 제거
        tokens = [t for t in tokens if t]
        
        # 콤마로 연결하고 끝에도 콤마 추가 (Local WAV 유도)
        return ", " + ", ".join(tokens) + ","