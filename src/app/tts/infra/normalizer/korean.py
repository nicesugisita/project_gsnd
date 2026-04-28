"""
한국어 텍스트 정규화 (korean.py)
"""

import re
from .base import BaseNormalizer
from .patterns.email import EmailHandler
from .patterns.phone import PhoneHandler
from .patterns.date import DateHandler
from .patterns.number import NumberHandler
from .patterns.digit_split import DigitSplitHandler
from .patterns.english_split import EnglishSplitHandler
from .patterns.korean_digit_split import KoreanDigitSplitHandler
from .patterns.alphanumeric import AlphanumericHandler
from .patterns.address import AddressHandler
from .patterns.mixed_code import MixedCodeHandler
from .patterns.url import UrlHandler
# from ..config_loader import get_config
# from saltlux.utils.logger import get_logger

class KoreanNormalizer(BaseNormalizer):
    def __init__(self, config: dict = None):
        super().__init__(config)
        # self.logger = get_logger("normalizer_debug")
        # 1. 핸들러 등록
        self._init_handlers()
        # 2. 후처리용 패턴 로딩
        # self._load_cleanup_patterns()

    def _init_handlers(self) -> None:
        """패턴 핸들러 초기화 및 등록"""
        # 우선순위에 따라 순서대로 실행됩니다.
        self.register_handler(KoreanDigitSplitHandler(self.config)) # 5 [추가] (한글 숫자 분리)
        self.register_handler(EmailHandler(self.config))      # 10
        self.register_handler(AddressHandler(self.config))    # 15
        self.register_handler(PhoneHandler(self.config))      # 20
        self.register_handler(DateHandler(self.config))       # 30
        self.register_handler(DigitSplitHandler(self.config)) # 50
        self.register_handler(AlphanumericHandler(self.config)) # 60
        self.register_handler(MixedCodeHandler(self.config))  #65
        self.register_handler(EnglishSplitHandler(self.config)) # 70
        self.register_handler(NumberHandler(self.config))     # 100
        self.register_handler(UrlHandler(self.config))     # 100


    # def _load_cleanup_patterns(self) -> None:
    #     """후처리용 정규식 컴파일 (Config 연동)"""
    #     p_multi_comma = get_config('patterns.cleanup.multi_comma', r'(?:,\s*){2,}')
    #     p_trailing_comma = get_config('patterns.cleanup.trailing_comma', r'\s*(?:,\s*)+$')
    #     p_multi_punc = get_config('patterns.cleanup.multi_punc', r'[.?!]{3,}')
    #     p_invalid = get_config('patterns.cleanup.invalid_chars', r'[^가-힣0-9 ,.!?]+')
    #
    #     self.re_multi_comma = re.compile(p_multi_comma)
    #     self.re_trailing_comma = re.compile(p_trailing_comma)
    #     self.re_multi_punc = re.compile(p_multi_punc)
    #     self.re_invalid = re.compile(p_invalid)
    #     self.re_whitespace = re.compile(r'\s+')

    def normalize(self, text: str) -> str:
        original_text = text
        # [Step 1] 전처리: 탭, 특수문자 리터럴 제거
        text = self._preprocess(text)
        # [디버그 로그] 시작
        # print(f"[Norm:Start] '{original_text}' -> Preprocessed: '{text}'")
        
        for handler in self.handlers:
            prev_text = text
            try:
                if handler.match(text):
                    text = handler.normalize(text)
                    # 텍스트가 변했을 때만 로그 출력
                    if prev_text != text:
                        print(f"[Norm:{handler.name}({handler.priority})] Changed: '{text}'")
                    else:
                        # 매칭은 됐는데 텍스트 변화가 없는 경우 (디버그용)
                         # self.logger.print_debug(f"[Norm:{handler.name}] Matched but No Change")
                         pass
            except Exception as e:
               print(f"[Norm:{handler.name}] Error: {e}")

        # =====================================================================
        # [신규] 최종 안전장치: 핸들러에서 처리 실패한 숫자를 한글로 강제 변환
        # NumberHandler에서 예외 발생 시 숫자가 그대로 남아있을 수 있음
        # 워커에 숫자가 넘어가면 오류가 발생하므로 여기서 최종 보장
        # =====================================================================
        text = self._ensure_no_digits(text)
                
        # [Step 3] 후처리
        before_post = text
        # text = self._postprocess(text)
        
        # if before_post != text:
        #     print(f"[Norm:PostProcess] '{text}'")
        return text

    def _preprocess(self, text: str) -> str:
        if not text: return ""

        # 1. 실제 제어 문자(Tab, Newline 등)를 공백으로 치환
        text = re.sub(r'[\t\n\r\v\f]+', ' ', text)

        # 2. [핵심 수정] 문자열 리터럴로 들어온 "\t", "\c", "\\" 등을 제거
        # 레거시 로직: text = re.sub(r'\\t|\\c|\\', ' ', text)
        # 파이썬 문자열에서 역슬래시 자체를 매칭하려면 \\\\가 필요함에 유의
        text = re.sub(r'\\t|\\c|\\', ' ', text)

        # 3. 괄호 안에 있는 한자나 날짜 등 제거 (레거시 유지)
        text = text.strip()
        text = re.sub(r'\(\d+일\)', '', text)
        text = re.sub(r'\([⺀-⺙⺛-⻳⼀-⿕々〇〡-〩〸-〺〻㐀-䶵一-鿃豈-鶴侮-頻並-龎]+\)', '', text)
        
        return text

    def _ensure_no_digits(self, text: str) -> str:
        """
        [신규] 최종 안전장치: 남은 숫자를 한자어 카디널로 강제 변환

        NumberHandler에서 예외가 발생하여 숫자가 변환되지 못한 경우,
        워커에 숫자가 그대로 전달되어 오류가 발생하는 것을 방지합니다.
        
        이 단계는 postprocess 전에 실행되어, 남은 숫자를 한글로 변환합니다.
        postprocess에서 특수문자(-, + 등)는 invalid_chars 패턴으로 제거됩니다.
        """
        if not re.search(r'\d', text):
            return text  # 숫자 없으면 패스

        print(f"[Norm:SafetyNet] 잔여 숫자 감지, 강제 변환 수행")

        # 한자어 숫자 변환 테이블
        num_to_kor1 = [""] + list("일이삼사오육칠팔구")
        num_to_kor2 = [""] + list("만억조경해")
        num_to_kor3 = [""] + list("십백천")
        digit_map = {'0': '영', '1': '일', '2': '이', '3': '삼', '4': '사',
                     '5': '오', '6': '육', '7': '칠', '8': '팔', '9': '구'}

        def _convert_cardinal(m: re.Match) -> str:
            """숫자 시퀀스를 한자어 카디널로 변환"""
            num_str = m.group()
            try:
                n = int(num_str)
                if n == 0:
                    return '영'
                str_val = str(n)
                size = len(str_val)
                kor_list = []
                for i, ch in enumerate(str_val):
                    v = int(ch)
                    pos = size - (i + 1)
                    if v != 0:
                        if v == 1 and pos % 4 != 0:
                            pass  # 십, 백, 천 앞의 1 생략
                        else:
                            kor_list.append(num_to_kor1[v])
                        kor_list.append(num_to_kor3[pos % 4])
                    if pos % 4 == 0 and pos > 0:
                        chunk = str_val[max(0, i - 3):i + 1]
                        if int(chunk) > 0:
                            kor_list.append(num_to_kor2[pos // 4])
                return ''.join(kor_list)
            except (ValueError, OverflowError, Exception):
                # 최후의 수단: 한 글자씩 변환
                return ' '.join(digit_map.get(c, c) for c in num_str)

        return re.sub(r'\d+', _convert_cardinal, text)

    def _postprocess(self, text: str) -> str:
        """
        후처리: 공백, 구두점, 콤마 정리
        """
        if not text: return ""

        # 1. 연속 공백 -> 단일 공백
        text = self.re_whitespace.sub(' ', text)

        if text.strip() == ',':
            return ','
        
        # 2. 연속 구두점 정리 (콤마 제외)
        text = self.re_multi_punc.sub('', text)

        # 3. 비허용 문자 제거
        text = self.re_invalid.sub('', text)

        # 4. 문장 중간 연속 콤마(2개 이상) -> 단일 콤마
        text = self.re_multi_comma.sub(', ', text)

        # 5. 문장 끝 콤마 -> 온점(.)으로 변경 (단, 쉼표만 있는 경우는 제외해야 함)
        # 여기서는 strip() 후 마지막 문자 체크하므로 안전
        text = self.re_trailing_comma.sub('.', text)

        text = text.strip()

        # 6. 마침표 보정 (문장이 비어있지 않고 끝이 구두점이 아니면 온점 추가)
        if text and text[-1] not in ['.', '!', '?', ',']:
            text += '.'

        return text