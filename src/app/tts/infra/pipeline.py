"""
전처리 파이프라인 (PreprocessingPipeline)

전체 흐름: 
Parser -> Normalizer -> Classifier -> (Final Segment)
"""

import re
from typing import List
import logging
import time

from dataclasses import dataclass

from .parser import TextParser, ParsedSegment
from .normalizer.korean import KoreanNormalizer
from .segmenter.classifier import SegmentClassifier
from .segmenter.merger import SegmentMerger

logger = logging.getLogger(__name__)

# 최종 출력용 세그먼트 (Engine/AudioGen 입력용)
@dataclass
class Segment:
    text: str
    seg_type: str = 'tts' # 'tts' | 'digit'
    tempo: float = 1.0
    break_after: float = 0.0
    gain_db: float = 0.0
    # 필요시 wav_key 추가 가능 (현재는 text가 곧 key 역할 하기도 함)

class PreprocessingPipeline:
    def __init__(self, config_path: str = None):
        # 각 모듈 초기화 (설정은 내부적으로 ConfigLoader 사용)
        self.parser = TextParser()
        self.normalizer = KoreanNormalizer()
        self.classifier = SegmentClassifier()
        # self.logger = get_logger("master")
        self.merger = SegmentMerger()
        #self.dict_manager = DictionaryManager()
        
    def process(self, text: str) -> List[Segment]:
        """
        입력 텍스트를 처리하여 최종 실행 가능한 Segment 리스트로 변환
        
        Args:
            text: 원본 텍스트 또는 SSML 문자열
            is_ssml: SSML 파싱 여부 (API 단에서 헤더 보고 결정해서 넘겨줌)
        전체 전처리 파이프라인 실행
        Flow: Parse -> Merge -> Dict Apply -> Normalize -> Classify
        """
        logger.info("==============================start to preprocess TTS Text ====================================================")
        start_time = time.time()

        # [Phase 1] 파싱 (text -> Segment Objects)
        parsed_segments = self.parser.parse(text)
        logger.info(f"텍스트 후처리후 : {parsed_segments}")
        # [Phase 1.5] 숫자+단위 병합 (Merging)
        # 예: ("62", ...) + ("개", ...) -> ("62개", ...)
        merged_segments = self.merger.merge(parsed_segments)

        logger.info(f"텍스트 병합후 : {merged_segments}")
        final_result: List[Segment] = []

        # [Phase 2 & 3] 정규화(Normalization) & 분류(Classification) 루프
        for i, seg in enumerate(merged_segments):
            # -----------------------------------------------------------
            # [Step 3] 사전 적용 (Dictionary Apply)
            # 마스킹 -> 사전 치환(Sentence/Term/Etc) -> 복원
            # -----------------------------------------------------------
            # dict_applied_text = self.dict_manager.apply(seg.text)
            # print(f"   사전 적용 후  : {dict_applied_text}")
            # 2.1 정규화 (Normalization)
            # 예: "010" -> "공, 일, 공."
            # 예: "json" -> "제이, 에스, 오, 엔."
            clean_text = self.normalizer.normalize(seg.text)
            # print(f"   노말라이즈 후  : {clean_text}")
            # 텍스트가 사라졌으면(특수문자 제거 등) 패스하되, break가 있으면 보존해야 함
            # (Classifier 내부에서 처리하도록 넘길 수도 있지만, 여기서 미리 거르면 효율적)
            # ==================================================================
            # [★핵심 수정] 500자 초과 시 강제 분할 (Safe Split)
            # ==================================================================
            # text_token_splitter 내부 로직:
            # 1. check_text_length(clean_text, 298자) 호출
            # 2. 298자 이하면 -> [clean_text] 그대로 리턴 (변형 없음)
            # 3. 298자 초과면 -> 온점(.) > 쉼표(,) > 공백( ) 우선순위로 안전하게 자름
            split_texts = text_token_splitter(clean_text, 298)
            
            # 분할이 일어났는지 로그로 확인
            if len(split_texts) > 1:
                logger.info(f"   [Split] 298자 초과 감지 -> {len(split_texts)}개로 분할됨.")

            for split_idx, sub_text in enumerate(split_texts):
                
                # 빈 텍스트 처리 (단, 원본의 break_after는 마지막 조각에서 살려야 함)
                if not sub_text.strip() and seg.pad_silence <= 0:
                    continue

                # 3. 분류 (Local/TTS)
                classified_segments = self.classifier.split_and_classify(sub_text)

                # 분류 결과가 없으면(특수문자 제거 등) break_after만 챙기고 스킵
                if not classified_segments:
                    is_global_last = (split_idx == len(split_texts) - 1)
                    if is_global_last and seg.pad_silence > 0:
                        final_result.append(Segment(
                            text="", 
                            seg_type='tts', 
                            tempo=seg.tempo, 
                            break_after=seg.pad_silence, 
                            gain_db=seg.gain_db
                        ))
                    continue

                # 4. 결과 매핑
                count = len(classified_segments)
                for j, sub_seg in enumerate(classified_segments):
                    # break_after 상속 로직:
                    # (1) 현재 텍스트 덩어리가 split_texts의 마지막이어야 하고
                    # (2) 현재 분류 조각이 classified_segments의 마지막이어야 함
                    # -> 그래야 원래 문장 맨 뒤에 있던 쉬는 시간이 유지됨
                    is_chunk_last = (split_idx == len(split_texts) - 1)
                    is_sub_last = (j == count - 1)
                    
                    final_break = seg.pad_silence if (is_chunk_last and is_sub_last) else 0.0

                    log_type = "LOCAL (WAV캐시)" if sub_seg.seg_type == 'digit' else "WORKER (추론)"
                    logger.info(
                        f"   [HYBRID: {i}-{split_idx}-{j}] 타입:{log_type:<13} | 내용: '{sub_seg.text}'"
                    )

                    final_result.append(Segment(
                        text=sub_seg.text,
                        seg_type=sub_seg.seg_type,
                        tempo=seg.tempo,
                        break_after=final_break,
                        gain_db=seg.gain_db
                    ))
            # ==================================================================
            end_time = time.time()
            elapsed_time = end_time - start_time
            logger.info(
                f"=================End to preprocess TTS Text = {elapsed_time:.2f} ==================================")
        return final_result
    
    def update_dictionary(self, term_dic: dict, replace: bool = False):
        self.dict_manager.update_dictionary(term_dic, replace)

    def reload_dictionary(self):
        self.dict_manager.reload()


###################################################################################################
# 298 초과 시 강제 분할 (Safe Split)
# text_token_splitter 내부 로직:
# 1. check_text_length(clean_text, 298) 호출
# 2. 298자 이하면 -> [clean_text] 그대로 리턴 (변형 없음)
# 3. 298자 초과면 -> 온점(.) > 쉼표(,) > 공백( ) 우선순위로 안전하게 자름
####################################################################################################
def text_token_splitter(text: str, max_len: int = 298) -> List[str]:
    """
    텍스트를 최대 길이(max_len) 이하로 안전하게 분할합니다.
    분할 우선순위: 온점(.) > 쉼표(,) > 공백( )
    """
    text = text.strip()

    # 1. 길이 체크: 기준 이하면 그대로 반환
    if len(text) <= max_len:
        return [text]

    # 2. 300자 초과 시 분할 지점 탐색
    # [우선순위 1] 온점(.) - 뒤에 공백이 있거나 문장의 끝인 경우 (이메일/소수점 보호)
    split_pos = _find_split_position(text, r'\.(?:\s|$)', max_len)

    # [우선순위 2] 쉼표(,) - 온점으로 안 잘릴 경우
    if split_pos == -1:
        split_pos = _find_split_position(text, r',(?:\s|$)', max_len)

    # [우선순위 3] 공백( ) - 쉼표로도 안 잘릴 경우
    if split_pos == -1:
        split_pos = _find_split_position(text, r'\s+', max_len)

    # [최후의 수단] 위 모든 조건으로도 분할 지점을 못 찾으면 강제로 글자수로 자름
    if split_pos == -1:
        split_pos = max_len

    # 분할 실행
    left_part = text[:split_pos].strip()
    right_part = text[split_pos:].strip()

    # 재귀적으로 뒷부분도 다시 검사 (남은 부분이 여전히 500자보다 길 수 있으므로)
    result = [left_part]
    result.extend(text_token_splitter(right_part, max_len))

    return [s for s in result if s]  # 빈 문자열 제거


def _find_split_position(text: str, pattern: str, max_len: int) -> int:
    """
    제한 길이(max_len) 내에서 가장 뒤에 있는 패턴의 위치를 반환합니다.
    """
    matches = list(re.finditer(pattern, text[:max_len]))
    if not matches:
        return -1

    # 가장 마지막 매칭 지점의 끝 인덱스 반환
    return matches[-1].end()

