"""
세그먼트 분할 및 분류 서브패키지

텍스트를 세그먼트로 분할하고 처리 타입을 분류합니다.

기존 파일 참조:
- saltlux/utils/function.py (split_segment_by_worker_type)
- saltlux/utils/text_to_token_splitter.py (문장 분리)

주요 기능:
- splitter: 문장/세그먼트 분할
- classifier: digit/tts 타입 분류
"""

from .splitter import SegmentSplitter, split_sentences
from .classifier import SegmentClassifier

__all__ = [
    'SegmentSplitter',
    'split_sentences',
    'SegmentClassifier',
]
