"""
세그먼트 병합기 (SegmentMerger)

파싱된 세그먼트 리스트에서 '숫자' + '단위' 구조를 찾아 하나로 병합합니다.
예: ("0000062", ...) + ("개", ...) -> ("0000062개", ...)

참조: 레거시 korean.py의 merge_numeric_segments 함수
"""

from typing import List
from dataclasses import replace
from ..parser import ParsedSegment
from ..config_loader import get_config

class SegmentMerger:
    def __init__(self):
        # normalization.yaml에서 고유어/한자어 단위를 모두 가져와 합칩니다.
        native = get_config('normalization.number_reading.native_units', [])
        sino = get_config('normalization.number_reading.sino_units', [])
        
        # 단위 목록 (개, 명, 살, 번, 회 ...)
        self.units = set(native + sino)

    def merge(self, segments: List[ParsedSegment]) -> List[ParsedSegment]:
        """
        숫자 세그먼트와 단위 세그먼트를 병합

        Args:
            segments: SSML 파싱 결과 리스트

        Returns:
            병합된 세그먼트 리스트
        """
        if not segments:
            return []

        merged_list = []
        i = 0
        n = len(segments)

        while i < n:
            curr_seg = segments[i]
            curr_text = curr_seg.text
            
            # 마지막 요소가 아니면 다음 요소 확인
            if i + 1 < n:
                next_seg = segments[i+1]
                next_text = next_seg.text.strip()

                # [병합 조건] 0107수정
                # 1. 현재 텍스트가 숫자(Digits)로만 구성되어 있음 (공백, 콤마 제거 후 확인)
                # 2. 다음 텍스트가 '단위'로 시작함
                clean_curr = curr_text.replace(',', '').replace('|', '').strip()
                
                if clean_curr.isdigit() and self._startswith_unit(next_text):
                    # 병합 실행: "0000062" + "개" -> "0000062개"
                    new_text = curr_text.replace('|', '') + next_seg.text # 공백 없이 붙임 (그래야 NumberHandler가 인식)
                    
                    # 속성 결정 (레거시 로직 준수)
                    # - Tempo/Gain: 앞쪽(숫자)의 속성 사용 (숫자의 prosody가 단위까지 적용됨)
                    # - Break: 뒤쪽(단위)의 break 사용 (단위 뒤에 쉬는 것이 자연스러움)
                    merged_seg = replace(
                        curr_seg,
                        text=new_text,
                        pad_silence=next_seg.pad_silence
                        # tempo, gain_db는 curr_seg 것 그대로 유지
                    )
                    
                    merged_list.append(merged_seg)
                    i += 2  # 두 개를 합쳤으므로 인덱스 2 증가
                    continue

            # 병합 조건이 아니면 그대로 추가
            merged_list.append(curr_seg)
            i += 1

        return merged_list

    def _startswith_unit(self, text: str) -> bool:
        """텍스트가 단위로 시작하는지 확인"""
        if not text:
            return False
        # "개입니다" 처럼 조사와 붙어있을 수 있으므로 startswith 체크
        for unit in self.units:
            if text.startswith(unit):
                return True
        return False