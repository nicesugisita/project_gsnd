"""
Token Manager

wav_mappings.yaml 설정을 로드하여
텍스트 토큰이 Local WAV(Digit) 처리가 가능한지 확인합니다.
"""

from typing import Dict, Optional
from ..config_loader import get_config
class TokenManager:
    def __init__(self):
        self._load_mappings()

    def _load_mappings(self):
        """설정 파일에서 매핑 정보 로드"""
        self.local_map: Dict[str, str] = {}
        
        # 1. Digits (영 -> 0)
        digits = get_config('wav_mappings.digits', {})
        self.local_map.update(digits)
        
        # 2. Alphabet (에이 -> a)
        alphabets = get_config('wav_mappings.alphabet', {})
        self.local_map.update(alphabets)
        
        # 3. Special (골뱅이 -> at)
        special = get_config('wav_mappings.special', {})
        self.local_map.update(special)
        
        # wav_mappings.yaml에 새로 정의한 'domains' 섹션을 로드해야 합니다.
        domains = get_config('wav_mappings.domains', {})
        self.local_map.update(domains)

    def get_wav_key(self, text: str) -> Optional[str]:
        """
        토큰 텍스트에 대응하는 WAV 파일 키를 반환합니다.
        Local 처리가 불가능하면 None을 반환합니다.
        
        Args:
            text: 검사할 텍스트 (예: "공", "에이", "안녕하세요")
            
        Returns:
            WAV 키 (예: "0", "a", "at") 또는 None
        """
        clean_text = text.strip().rstrip('.!?') # 문장부호 제거 후 키 검색
        
        # 1. 매핑 테이블에 있는 경우 (한글 발음 -> 키)
        if clean_text in self.local_map:
            return self.local_map[clean_text]
            
        # 2. 텍스트 자체가 이미 키인 경우 (숫자 '0', 알파벳 'a' 등) - 레거시 호환
        # (단, wav_mappings에 정의되지 않은 것은 제외할지 정책 결정 필요. 여기선 유연하게 허용)
        if clean_text.isdigit() or (len(clean_text) == 1 and clean_text.encode().isalpha()):
             return clean_text
             
        return None

    def is_local(self, text: str) -> bool:
        """Local 처리 가능 여부 확인"""
        return self.get_wav_key(text) is not None