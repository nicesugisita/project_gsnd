import re
from typing import List
from dataclasses import replace
# 사용하시는 환경의 ParsedSegment 위치에 맞게 import 하세요.
# from normalizer.parser import ParsedSegment 

class SegmentMerger:
    """
    세그먼트 병합기 (SegmentMerger)
    
    기능: 
    - 분리된 '숫자' 세그먼트와 '단위' 세그먼트를 하나로 병합.
    - 예: ("핸드폰 2", ...) + ("개인 부자", ...) -> ("핸드폰 2개", ...) + ("인 부자", ...)
    - 병합을 통해 NumberHandler가 수사(두, 세, 네...)를 정확히 판별하도록 유도함.
    """

    def __init__(self):
        # 고유어 단위 (한, 두, 세... 변환용)
        native = ['개', '명', '살', '시', '마리', '송이', '채', '대', '자루', '통', '건', '그루', '켤레', '쌍', '벌']
        # 한자어 단위 (일, 이, 삼... 변환용)
        sino = ['번', '회', '회차', '분', '초', '일', '주', '년', '월', '원', '호', '층', '차', '기', '권', '장', '개월']
        
        # 전체 단위 목록 생성 (긴 단어 우선 매칭을 위해 정렬)
        self.units = sorted(list(set(native + sino)), key=len, reverse=True)

    def merge(self, segments: List['ParsedSegment']) -> List['ParsedSegment']:
        """
        SSML 파싱 결과 리스트를 순회하며 숫자와 단위를 병합합니다.
        """
        if not segments:
            return []

        merged_list = []
        i = 0
        n = len(segments)

        while i < n:
            curr_seg = segments[i]
            # 현재 세그먼트 텍스트 (불필요한 파이프 등 제거)
            curr_text = curr_seg.text.replace('|', '')
            
            # 다음 세그먼트가 존재할 때만 병합 로직 수행
            if i + 1 < n:
                next_seg = segments[i+1]
                next_text = next_seg.text.strip()

                # 1. 현재 세그먼트가 숫자로 끝나는지 확인 (\d+$)
                digit_match = re.search(r'\d+$', curr_text.strip())
                
                if digit_match:
                    # 2. 다음 세그먼트가 단위로 시작하는지 확인
                    unit_found = self._get_starting_unit(next_text)
                    
                    if unit_found:
                        # [병합 실행] 숫자 끝부분과 단위를 공백 없이 밀착
                        # .rstrip()으로 숫자 뒤 공백 제거 후 단위 결합
                        new_text = curr_text.rstrip() + unit_found
                        
                        # 병합된 세그먼트 생성 (속성은 현재 것 유지, 휴지기는 다음 것 유지)
                        merged_seg = replace(
                            curr_seg,
                            text=new_text,
                            pad_silence=next_seg.pad_silence
                        )
                        merged_list.append(merged_seg)

                        # 3. 단위 뒤에 남은 텍스트 처리 ("개인 부자" -> "인 부자")
                        remaining_text = next_text[len(unit_found):].strip()
                        if remaining_text:
                            # 남은 텍스트는 새로운 세그먼트로 만들어 다음 루프에서 처리되게 함
                            segments[i+1] = replace(next_seg, text=" " + remaining_text)
                            i += 1
                        else:
                            # 다음 세그먼트가 단위뿐이었다면 2개 세그먼트를 완전히 소모
                            i += 2
                        continue

            # 병합 조건이 아니면 그대로 추가
            merged_list.append(curr_seg)
            i += 1

        return merged_list

    def _get_starting_unit(self, text: str) -> str:
        """텍스트가 어떤 단위로 시작하는지 찾아서 반환"""
        if not text:
            return ""
        for unit in self.units:
            if text.startswith(unit):
                return unit
        return ""

    def _startswith_unit(self, text: str) -> bool:
        """텍스트가 단위로 시작하는지 여부 확인 (기존 인터페이스 유지용)"""
        return bool(self._get_starting_unit(text))