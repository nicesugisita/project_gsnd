"""
세그먼트 분류기 (SegmentClassifier)

정규화된 텍스트를 입력받아 TokenManager를 통해 타입을 확인하고,
Local WAV 구간과 Worker TTS 구간으로 분리(Split) 및 그룹핑합니다.
"""

from typing import List, Tuple
from dataclasses import dataclass
from .token_manager import TokenManager

# 파이프라인 내부용 세그먼트 (경량화)
@dataclass
class ClassifiedSegment:
    text: str
    seg_type: str        # 'tts' | 'digit'
    wav_key: str = None  # Local일 경우 파일 키

class SegmentClassifier:
    def __init__(self, config: dict = None):
        self.config = config or {}
        # 데이터 관리자 분리 (Dependency Injection 가능)
        self.token_manager = TokenManager()

    def split_and_classify(self, text: str) -> List[ClassifiedSegment]:
        """
        텍스트를 쉼표로 쪼개어 타입별로 그룹핑합니다.
        """
        if not text or not text.strip():
            return []
        # (쉼표만 있으면 split(',') 시 빈 리스트가 되기 때문)
        if text.strip() == ',':
            return [ClassifiedSegment(text=',', seg_type='tts')]
        # 1. 콤마(,) 기준으로 토큰 분리
        # Normalizer에서 이미 "일, 이, 삼," 처럼 콤마를 붙여줬으므로 쉼표로 쪼갭니다.
        raw_tokens = text.split(',')
        
        result_groups: List[ClassifiedSegment] = []
        
        current_group_tokens = []
        current_type = None  # 'digit' or 'tts'

        for token in raw_tokens:
            token_str = token.strip()
            if not token_str: continue # 빈 토큰 스킵
            
            # TokenManager에게 물어봄 (여기가 핵심)
            wav_key = self.token_manager.get_wav_key(token_str)
            token_type = 'digit' if wav_key else 'tts'
            # [디버그 추가] wav_key가 무엇으로 잡혔는지 확인
            if token_str in ["네이버", "닷", "컴", "알파벳이"]:
                print(f"[DEBUG:Classifier] 토큰: '{token_str}' -> Key: '{wav_key}'")
            # --- 그룹핑 로직 (연속된 같은 타입 묶기) ---
            if current_type is None:
                current_type = token_type
                current_group_tokens.append(token_str)
            
            elif current_type == token_type:
                current_group_tokens.append(token_str)
                
            else:
                # 타입이 바뀌면 이전 그룹 저장
                self._flush_group(result_groups, current_group_tokens, current_type)
                
                # 새 그룹 시작
                current_type = token_type
                current_group_tokens = [token_str]

        # 마지막 그룹 저장
        self._flush_group(result_groups, current_group_tokens, current_type)

        return result_groups

    def _flush_group(self, results: list, tokens: list, type_str: str):
        """그룹을 텍스트로 합쳐서 결과 리스트에 추가"""
        if not tokens: return
        
        # TTS 타입은 문장처럼 다시 연결 (콤마 복원)
        # Digit 타입도 일단은 텍스트로 넘기되, 추후 wav 생성 시 다시 쪼개거나 
        # 아니면 여기서부터 개별 Segment로 만들 수도 있음.
        # 레거시 호환성을 위해 "콤마로 연결된 문자열" 상태 유지.
        combined_text = ", ".join(tokens)
        
        # WAV Key는 그룹 전체에 대해 하나로 정의하기 어려우므로 None (개별 처리는 후속 단계)
        results.append(ClassifiedSegment(
            text=combined_text,
            seg_type=type_str
        ))