"""
이메일 패턴 핸들러

이메일 주소를 한국어 발음으로 변환합니다.

기존 코드 참조: saltlux/text/korean.py
- EMAIL_PATTERN (line 112)
- MESSY_EMAIL_PATTERN (line 159)
- SPOKEN_EMAIL_PATTERN (line 160)
- email_to_korean(), messy_email_to_korean(), spoken_email_to_korean()
- _make_email_pronunciation()

설정 파일: saltlux/config/domains.yaml, normalization.yaml
"""

import re
from typing import Dict
from ..base import PatternHandler
#from config_loader import get_config
# from saltlux.utils.logger import get_logger


class EmailHandler(PatternHandler):
    """
    이메일 패턴 핸들러

    이메일 주소를 감지하고 한국어 발음으로 변환합니다.
    """

    def __init__(self, config: dict = None):
        """
        Args:
            config: 추가 설정 (선택적)
        """
        # self.logger = get_logger("email_debug")
        self._config = config or {}
        self._load_config()
        self._compile_patterns()

    def _load_config(self) -> None:
        """YAML 설정 파일 대신 코드 내부에 직접 정의"""

        # 1. 도메인 발음 사전 (domains.yaml 대체)
        self.domain_dict: Dict[str, str] = {
            'naver.com': '네이버, 닷, 컴',
            'gmail.com': '지메일, 닷, 컴',
            'daum.net': '다음, 닷, 넷',
            'nate.com': '네이트, 닷, 컴',
            'outlook.com': '아웃룩, 닷, 컴',
            'saltlux.com': '솔트룩스, 닷, 컴'
        }

        # 2. 알파벳 발음 (normalization.yaml 대체)
        self.alphabet_dict: Dict[str, str] = {
            'a': '에이', 'b': '비', 'c': '씨', 'd': '디', 'e': '이', 'f': '에프',
            'g': '지', 'h': '에이치', 'i': '아이', 'j': '제이', 'k': '케이', 'l': '엘',
            'm': '엠', 'n': '엔', 'o': '오', 'p': '피', 'q': '큐', 'r': '알',
            's': '에스', 't': '티', 'u': '유', 'v': '브이', 'w': '더블유', 'x': '엑스',
            'y': '와이', 'z': '지'
        }

        # 3. 숫자 발음
        self.digit_dict: Dict[str, str] = {
            '0': '영', '1': '일', '2': '이', '3': '삼', '4': '사',
            '5': '오', '6': '육', '7': '칠', '8': '팔', '9': '구'
        }

        # 4. 특수문자 발음
        self.special_dict: Dict[str, str] = {
            '@': '골뱅이',
            '.': '쩜',
            '-': '다시',
            '_': '언더바',
            '%': '퍼센트',
            '+': '플러스'
        }

        # 5. 이메일 추출 정규식 패턴 (patterns.yaml 대체)
        self.email_pattern_str = r'([a-zA-Z0-9._%+\-~가-힣]+)@([a-zA-Z0-9.\-가-힣]+\.[a-zA-Z가-힣]{2,})'
        self.messy_pattern_str = r'([-a-zA-Z0-9\s,._쩜가-힣\\]+?)[\s,]*골뱅이[\s,]*([-a-zA-Z0-9\s,._쩜\\]+)'
        self.spoken_pattern_str = r'([a-zA-Z0-9._%+\-~쩜가-힣]+)\s*골뱅이\s*([a-zA-Z0-9.\-가-힣\s]+?쩜\s*[a-zA-Z0-9.\-가-힣]+)'


    def _compile_patterns(self) -> None:
        """정규표현식 컴파일"""
        self.email_pattern = re.compile(self.email_pattern_str)
        self.messy_pattern = re.compile(self.messy_pattern_str)
        self.spoken_pattern = re.compile(self.spoken_pattern_str)
        # 공백 제거용 정규식 (탭, 줄바꿈 포함)
        self.whitespace_pattern = re.compile(r'\s+')

    @property
    def name(self) -> str:
        return "email"

    @property
    def priority(self) -> int:
        return 10  # 높은 우선순위 (먼저 처리)

    def match(self, text: str) -> bool:
        """이메일 패턴 존재 여부 확인"""
        return bool(
            self.email_pattern.search(text) or
            self.messy_pattern.search(text) or
            self.spoken_pattern.search(text)
        )

    def normalize(self, text: str) -> str:
        """
        이메일을 한국어 발음으로 변환

        처리 순서:
        1. 구어체 이메일 (골뱅이 키워드 포함)
        2. 지저분한 이메일 (공백/콤마 포함)
        3. 표준 이메일
        """
        # 1. 구어체 이메일 처리 (예: "테스트 골뱅이 네이버 쩜 컴")
        text = self.spoken_pattern.sub(self._spoken_email_to_korean, text)

        # 2. 지저분한 이메일 처리 (예: "테스트, 골뱅이, 네이버")
        text = self.messy_pattern.sub(self._messy_email_to_korean, text)

        # 3. 표준 이메일 처리 (예: "test@naver.com")
        text = self.email_pattern.sub(self._email_to_korean, text)

        return text

    def _char_to_korean(self, char: str) -> str:
        """
        단일 문자를 한국어 발음으로 변환 + [Debug Log]
        """
        char_lower = char.lower()
        val = char  # 기본값
        source = "raw"
        # 1. 점(.) 처리
        if char == '.':
            val = '쩜'
            source = "hardcoded"
        # 2. 알파벳
        elif char_lower in self.alphabet_dict:
            val = self.alphabet_dict[char_lower]
            source = "alphabet_dict"
        # 3. 숫자
        elif char in self.digit_dict:
            val = self.digit_dict[char]
            source = "digit_dict"
        # 4. 특수문자
        elif char in self.special_dict:
            val = self.special_dict[char]
            source = "special_dict"
        # 5. 한글은 그대로
        elif '\uac00' <= char <= '\ud7a3':
            val = char
            source = "hangul"
        # [디버그 1] 원본 값 확인 (필요시 주석 해제)
        # if char_lower == 'h':
        #    print(f"[DBG:Char] '{char}' -> '{val}' (Source: {source})")
        # [핵심 방어 로직] 
        # 1. 먼저 문자열로 변환
        str_val = str(val)
        # 2. 콤마와 공백 제거하여 final_val 생성
        final_val = str_val.replace(',', '').replace(' ', '').strip()
        # [디버그 2] 방어 로직이 실제로 콤마를 지웠는지 확인
        # "에, 이, 치" -> "에이치"로 변했을 때만 로그 출력
        if char_lower == 'h' and final_val != str_val:
           print(f"[DBG:Fix] Cleaned '{str_val}' -> '{final_val}'")
        # 3. 최종 값 리턴
        return final_val

    def _make_email_pronunciation(self, local_part: str, domain_part: str) -> str:
        """
        이메일 발음 생성 (레거시 로직 재현)
        - ID 부분은 무조건 한 글자씩 쪼개고 콤마를 붙여 Local WAV를 타게 함.
        - 도메인 부분은 사전 매칭 시 해당 발음, 미매칭 시 낱글자 분리.
        """
        # [수정 시작] 1. ID 파트 처리 -----------------------------------------
        
        # 쪼개지 않고 붙여서 발음해야 할 한글 키워드 정의
        # 필요시 ["물결", "느낌표", "샵"] 등으로 추가 가능
        keep_together_words = ["물결", "샾", "대시", "다시", "느낌표", "언더바", "샾"]
        
        # 정규식 패턴 생성: "물결"을 먼저 찾고, 아니면 한 글자(.)를 찾음
        # 예: "a물결b" -> ['a', '물결', 'b'] 로 분리됨
        tokenize_pattern = "|".join(keep_together_words) + "|."
        
        # 위 패턴을 이용해 local_part를 토큰 리스트로 분리
        tokens = re.findall(tokenize_pattern, local_part)
        
        local_kor = []
        for token in tokens:
            if token in keep_together_words:
                # 보호해야 할 단어는 변환/분리 없이 그대로 리스트에 추가
                # 이렇게 하면 나중에 join 될 때 "..., 물결, ..." 형태로 한 덩어리가 됨
                local_kor.append(f"{token}.")
            else:
                # 일반 문자(알파벳, 숫자 등)는 기존 로직대로 변환
                converted = self._char_to_korean(token)
                local_kor.append(converted)
        # [디버그 4] 조인 전 리스트 확인 (여기서 ['에', '이', '치'] 인지 ['에이치'] 인지 판별됨)
        print(f"[DBG:List] {local_kor}")
        
        # [핵심] 리스트 요소 사이사이에 콤마 삽입
        local_res = ", ".join(local_kor)

        # 2. Domain 파트 처리
        domain_lower = domain_part.lower()
        
        if domain_lower in self.domain_dict:
            # [Case A] 사전에 있는 도메인 (예: naver.com -> "네이버, 닷, 컴")
            # 사전값 자체가 이미 콤마를 포함하고 있다고 가정
            domain_res = self.domain_dict[domain_lower]
        else:
            # [Case B] 사전에 없는 도메인 -> ID처럼 한 글자씩 쪼개기
            domain_kor = [self._char_to_korean(c) for c in domain_part]
            domain_res = ", ".join(domain_kor)

        # 최종 조합: ID, 골뱅이, 도메인
        # 사이사이 콤마를 넣어 SegmentClassifier가 끊어줄 수 있게 함
        result = f"{local_res}, 골뱅이, {domain_res}"
        
        # [디버그 5] 최종 결과 확인
        print(f"[DBG:Final] {result}")
        return result

    def _email_to_korean(self, match: re.Match) -> str:
        """표준 이메일 패턴 처리"""
        local_part = match.group(1)
        domain_part = match.group(2)
        return self._make_email_pronunciation(local_part, domain_part)

    def _messy_email_to_korean(self, match: re.Match) -> str:
        """
        지저분한 이메일 패턴 처리 (공백/콤마/탭 포함)
        SSML 태그가 벗겨진 후 남은 \t, 공백, 연속 콤마 등을 강력하게 제거합니다.
        """
        # [Fix] 방어 로직 추가: 이미 spoken_pattern 등에 의해 변환된 결과인지 확인
        # _make_email_pronunciation 결과물은 ", 골뱅이, " 패턴을 가집니다.
        full_match = match.group(0)
        if ", 골뱅이," in full_match:
            # 이미 변환된 한글 문자열이 재매칭된 것이므로 원본 그대로 반환
            return full_match
        raw_id = match.group(1)
        raw_domain = match.group(2)

        # [수정] 탭(\t), 줄바꿈(\n) 등 모든 공백 제거 (regex \s+)
        # 1. ID 정제: 콤마 제거, 공백 제거, '쩜' -> '.' 변환
        clean_id = self.whitespace_pattern.sub('', raw_id)
        clean_id = clean_id.replace(',', '').replace('쩜', '.')

        # 2. 도메인 정제
        clean_domain = self.whitespace_pattern.sub('', raw_domain)
        clean_domain = clean_domain.replace(',', '').replace('쩜', '.')

        return self._make_email_pronunciation(clean_id, clean_domain)

    def _spoken_email_to_korean(self, match: re.Match) -> str:
        """구어체 이메일 패턴 처리"""
        local_part = match.group(1)
        domain_part = match.group(2)

        # 정제
        clean_local = self.whitespace_pattern.sub('', local_part)
        clean_local = clean_local.replace(",", "").replace("쩜", ".")
        
        clean_domain = self.whitespace_pattern.sub('', domain_part)
        clean_domain = clean_domain.replace(",", "").replace("쩜", ".")

        return self._make_email_pronunciation(clean_local, clean_domain)