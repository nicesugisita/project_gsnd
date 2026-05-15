import re
from typing import Dict, Set
from ..base import PatternHandler
from ...config_loader import get_config
#from saltlux.utils.logger import get_logger

class AddressHandler(PatternHandler):
    """
    주소 패턴 핸들러
    
    설정 파일(patterns.yaml)에 정의된 규칙을 기반으로 주소를 탐지하고 정규화합니다.
    행정구역 체계(Strict Hierarchy)를 준수하며, 번지수와 상세주소를 분리 처리합니다.
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        # self.logger = get_logger("address_debug")
        
        self._load_config()
        self._compile_patterns()
        
        # 변환용 사전 및 헬퍼 초기화
        self._init_helpers()

    def _load_config(self) -> None:
        # 1. 행정구역 단위 로딩 (동일)
        self.major_units = get_config('patterns.address.units.major', r'(?:특별시|광역시|특별자치시|자치시|도|시|군|구)')
        self.minor_units = get_config('patterns.address.units.minor', r'(?:읍|면|동|가|리|로|길|대로)')

        # 2. 붙여쓰기 단위 (동일)
        tight_list = get_config('patterns.address.tight_suffixes', ['동', '호', '층', '번지', '통', '반', '가', '길', '로', '리'])
        self.tight_suffixes: Set[str] = set(tight_list)

        # [핵심 수정 3] 정규식 템플릿 제약 강화
        # 1) Prefix: 주소 단위(시/도/로/길) 뒤에 '으로', '에서' 같은 조사가 붙으면 매칭하지 않도록 전방부정탐색(?!...) 추가
        #    또한 도로명 뒤에는 보통 공백이나 숫자가 오므로 단어 경계 조건을 강화합니다.
        self.regex_prefix_tpl = get_config(
            'patterns.address.regex.prefix',
            r'((?:[가-힣0-9]+(?:{major}|{minor})(?![가-힣])\s+)*[가-힣0-9]+(?:{major}|{minor})(?![가-힣]))'
        )

        # 2) Number: 번지수 뒤에 하이픈이 연속 2개 이상 나오는 형태(전화번호 형식)는 번지수로 잡지 않도록 차단(?!.*\d+-\d+)
        self.regex_number_str = get_config(
            'patterns.address.regex.number',
            r'((?!.*\d+-\d+-\d+)(?:산\s*)?\d+(?:[\s-]*\d+)*(?:\s*(?:번지|호|동|층|가|통|반))?)'
        )

        # 3) Suffix (기존과 동일)
        self.regex_suffix_str = get_config(
            'patterns.address.regex.suffix',
            r'((?:\s+\d+[가-힣A-Za-z0-9\s]*?(?:동|호|층|빌딩|아파트|상가))?)(?:\s*\(([^)]+)\))?'
        )

        # 4) Building (기존과 동일)
        self.regex_building_str = get_config(
            'patterns.address.regex.building',
            r'([가-힣0-9a-zA-Z]*[a-zA-Z]+[가-힣0-9a-zA-Z]*)\s*(?:아파트|빌라|맨션|타워|오피스텔|동|호|층|점|빌딩)'
        )

        # 5. 알파벳 발음 (기존과 동일)
        raw_alphabet = get_config('patterns.alphabet', get_config('normalization.alphabet', {}))
        self.alphabet_dict: Dict[str, str] = self._create_clean_alphabet_map(raw_alphabet)


    # [신규 추가] 알파벳 발음 정제 로직 (MixedCodeHandler와 동일 로직)
    def _create_clean_alphabet_map(self, raw_dict: Dict[str, str]) -> Dict[str, str]:
        """
        주소/건물명 문맥에 맞게 알파벳 발음을 정제합니다.
        예: '알파벳 오' -> '오', '쥐' -> '지'
        """
        clean_dict = raw_dict.copy()
        
        # [Override] 자연스러운 발음으로 덮어쓰기
        overrides = {
            'g': '지',  # G 타워 -> 지 타워
            'o': '오',  # passion5 -> 패션 오
            'e': '이',  # e편한세상 -> 이편한세상
            'z': '제트'
        }
        clean_dict.update(overrides)

        # "알파벳 " 접두어 제거
        for k, v in clean_dict.items():
            if "알파벳" in v:
                clean_dict[k] = v.replace("알파벳", "").strip()
                
        return clean_dict
    
    def _compile_patterns(self) -> None:
        """정규표현식 조립 및 컴파일"""
        # Prefix 패턴 내의 {major}, {minor} 치환
        prefix_pattern = self.regex_prefix_tpl.format(
            major=self.major_units,
            minor=self.minor_units
        )
        
        # Standard Address Pattern 조립
        # 구조: Prefix + 공백 + Number + Suffix
        self.pattern_std_str = f"{prefix_pattern}\\s+{self.regex_number_str}{self.regex_suffix_str}"
        
        self.pattern_std = re.compile(self.pattern_std_str)
        self.pattern_bld = re.compile(self.regex_building_str)
        self.suffix_num_pattern = re.compile(r'(\d[\d\s-]*)\s*([가-힣a-zA-Z]+)')

    def _init_helpers(self):
        """숫자 변환용 헬퍼 데이터 초기화"""
        self.num_to_kor1 = [""] + list("일이삼사오육칠팔구")
        self.num_to_kor2 = [""] + list("만억조경해")
        self.num_to_kor3 = [""] + list("십백천")

    @property
    def name(self) -> str:
        return "address"

    @property
    def priority(self) -> int:
        return 15

    def match(self, text: str) -> bool:
        return bool(self.pattern_std.search(text) or self.pattern_bld.search(text))

    def normalize(self, text: str) -> str:
        if self.pattern_bld.search(text):
            text = self.pattern_bld.sub(self._process_building, text)
        if self.pattern_std.search(text):
            text = self.pattern_std.sub(self._process_standard, text)
        return text

    def _process_standard(self, match: re.Match) -> str:
        try:
            # 그룹 인덱스는 정규식 구조에 따라 결정됨
            # 1: Prefix 전체
            # 2: Number 파트
            # 3: Suffix 파트 (없을 수 있음)
            # 4: 괄호 내용 (없을 수 있음)
            
            prefix = match.group(1).strip()
            num_part = match.group(2).strip()
            
            # lastindex 체크를 통해 안전하게 그룹 가져오기
            suffix = match.group(3) if match.lastindex and match.lastindex >= 3 else ""
            paren_content = match.group(4) if match.lastindex and match.lastindex >= 4 else None
            
            print(f"[Addr:Std] prefix='{prefix}', num='{num_part}', suffix='{suffix}', paren='{paren_content}'")

            # 한글 번지수 처리 판별 (숫자가 한글로 쓰여있는지 확인)
            if any(c in "영일이삼사오육칠팔구십백천만" for c in num_part):
                 converted_nums = num_part
            else:
                 converted_nums = self._convert_address_numbers(num_part)
            
            result = f"{prefix} {converted_nums}"
            
            # Suffix 처리
            if suffix and suffix.strip():
                clean_suffix = suffix.strip()
                processed_suffix = self._process_suffix_recursive(clean_suffix)
                
                # tight_suffixes 설정에 따라 붙여쓰기 결정
                # 예: "101동" (붙임), "지하 주차장" (띄움)
                should_attach = False
                for unit in self.tight_suffixes:
                    if clean_suffix.startswith(unit):
                        should_attach = True
                        break
                
                if should_attach:
                    result += processed_suffix
                else:
                    result += f" {processed_suffix}"
            
            # 괄호 처리
            if paren_content:
                result += f", {paren_content}"
            
            print(f"[Addr:Std] Converted: '{result}'")
            return result
            
        except Exception as e:
            self.logger.print_error(f"[Addr:Std] Error: {e}")
            return match.group(0)

    def _process_suffix_recursive(self, text: str) -> str:
        """상세주소 내의 숫자+단위 처리 (예: 3층 201호)"""
        def _repl(m):
            n_str = m.group(1).strip()
            u_str = m.group(2).strip()
            kor_num = self._convert_address_numbers(n_str).replace(" ", "") 
            
            if u_str in self.tight_suffixes:
                return f"{kor_num}{u_str}"
            else:
                return f"{kor_num} {u_str}"
        return self.suffix_num_pattern.sub(_repl, text)

    def _process_building(self, match: re.Match) -> str:
        """건물명 처리 (영어 -> 한글 발음)"""
        full_match = match.group(0)
        target_word = match.group(1)
        converted_word = self._convert_english_no_comma(target_word)
        return full_match.replace(target_word, converted_word)

    def _convert_english_no_comma(self, text: str) -> str:
        result = []
        for char in text:
            if 'a' <= char.lower() <= 'z':
                result.append(self.alphabet_dict.get(char.lower(), char))
            else:
                result.append(char)
        return "".join(result)

    def _convert_address_numbers(self, text: str) -> str:
        """번지수 변환 (하이픈 -> '다시')"""
        text = text.replace('-', ' 다시 ')
        tokens = text.split()
        result = []
        for token in tokens:
            if token == "다시":
                result.append("다시")
            elif token.isdigit():
                result.append(self._number_to_sino(int(token)))
            else:
                result.append(token)
        return " ".join(result)

    def _number_to_sino(self, n: int) -> str:
        """숫자 -> 한자어 수사 변환"""
        if n == 0: return "영"
        str_val = str(n)
        kor_list = []
        for i, v_char in enumerate(str_val):
            v = int(v_char)
            position = len(str_val) - (i + 1)
            if v != 0:
                if v == 1 and position % 4 != 0: pass
                elif v == 1 and position == 0: kor_list.append("일")
                else: kor_list.append(self.num_to_kor1[v])
                kor_list.append(self.num_to_kor3[position % 4])
            if position % 4 == 0 and position > 0:
                if int(str_val[max(0, i-3):i+1]) > 0:
                    kor_list.append(self.num_to_kor2[position // 4])
        return "".join(kor_list)