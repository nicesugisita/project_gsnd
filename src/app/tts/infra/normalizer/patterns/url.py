import re
from ..base import PatternHandler


class UrlHandler(PatternHandler):
    """
    URL 패턴 핸들러

    기능:
    1. https://, http:// 제거 또는 읽기 처리
    2. www -> 떠블유 떠블유 떠블유 변환
    3. 도메인 구분 기호(.) -> 쩜 변환
    4. 알파벳 -> 한국어 발음 변환
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        self._load_hardcoded_config()
        self._compile_patterns()

    def _load_hardcoded_config(self) -> None:
        """알파벳 및 기호 발음 사전 정의"""
        self.alphabet_dict = {
            'a': '에이', 'b': '비', 'c': '씨', 'd': '디', 'e': '이',
            'f': '에프', 'g': '지', 'h': '에이치', 'i': '아이', 'j': '제이',
            'k': '케이', 'l': '엘', 'm': '엠', 'n': '엔', 'o': '오',
            'p': '피', 'q': '큐', 'r': '알', 's': '에스', 't': '티',
            'u': '유', 'v': '브이', 'w': '더블유', 'x': '엑스', 'y': '와이', 'z': '제트',
        }

        # 특수 기호 발음
        self.symbol_dict = {
            '.': ' 쩜 ',
            '/': ' 슬래시 ',
            ':': ' 땡땡 ',
            '-': ' 다시 ',
            '_': ' 언더바 ',
        }

    def _compile_patterns(self) -> None:
        """URL 감지를 위한 정규표현식"""
        # http(s) 포함 또는 www로 시작하거나 .com, .kr 등으로 끝나는 패턴
        self.url_pattern = re.compile(
            r'(https?://[^\s<>"]+|www\.[^\s<>"]+\.[^\s<>"]+|[^\s<>"]+\.(?:kr|com|net|org|go\.kr|or\.kr))'
        )

    @property
    def name(self) -> str:
        return "url"

    @property
    def priority(self) -> int:
        # 이메일이나 일반 숫자보다 먼저 처리하기 위해 우선순위 조절 (낮을수록 빠름)
        return 15

    def match(self, text: str) -> bool:
        return bool(self.url_pattern.search(text))

    def normalize(self, text: str) -> str:
        """URL을 한국어 발음으로 변환"""
        return self.url_pattern.sub(self._url_to_korean, text)

    def _url_to_korean(self, match: re.Match) -> str:
        url_str = match.group().lower()

        # 1. 프로토콜 처리 (https:// 등은 생략하거나 읽어줌)
        # 여기서는 생략하지 않고 모두 읽는 방식으로 구현

        result = []
        for char in url_str:
            if char in self.alphabet_dict:
                result.append(self.alphabet_dict[char])
            elif char in self.symbol_dict:
                result.append(self.symbol_dict[char])
            elif char.isdigit():
                # 숫자는 기존 숫자 발음(공, 일, 이...) 사용 시도 (여기서는 단순 매핑)
                digit_map = {'0': '공', '1': '일', '2': '이', '3': '삼', '4': '사', '5': '오', '6': '육', '7': '칠', '8': '팔',
                             '9': '구'}
                result.append(digit_map.get(char, char))
            else:
                result.append(char)

        # 2. '더블유 더블유 더블유'의 경우 '떠블유'로 발음하는 경우가 많아 보정 (선택 사항)
        converted = "".join(result)
        converted = converted.replace("더블유더블유더블유", "떠블유 떠블유 떠블유")

        return converted