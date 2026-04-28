"""
세그먼트 분할기

텍스트를 문장 또는 세그먼트 단위로 분할합니다.

기존 코드 참조:
- saltlux/utils/text_to_token_splitter.py
- saltlux/text/frontend_utils.py (split_paragraph)
"""

import re
from typing import List
from dataclasses import dataclass


@dataclass
class TextSegment:
    """
    텍스트 세그먼트

    Attributes:
        text: 세그먼트 텍스트
        start_idx: 원본 텍스트에서의 시작 위치
        end_idx: 원본 텍스트에서의 끝 위치
    """
    text: str
    start_idx: int = 0
    end_idx: int = 0


class SegmentSplitter:
    """
    세그먼트 분할기

    텍스트를 적절한 단위로 분할합니다.
    """

    # 문장 종결 패턴
    SENTENCE_END_PATTERN = re.compile(r'[.!?。]+')

    # 분할 기준 문자
    SPLIT_CHARS = ['.', '!', '?', ',', '。']

    def __init__(self, config: dict = None):
        """
        Args:
            config: 분할 설정
                - max_length: 최대 세그먼트 길이
                - split_by_comma: 쉼표로 분할 여부
        """
        self.config = config or {}
        self.max_length = self.config.get('max_length', 200)
        self.split_by_comma = self.config.get('split_by_comma', True)

    def split(self, text: str) -> List[TextSegment]:
        """
        텍스트를 세그먼트로 분할

        Args:
            text: 원본 텍스트

        Returns:
            TextSegment 리스트
        """
        raise NotImplementedError()

    def split_by_sentence(self, text: str) -> List[str]:
        """
        텍스트를 문장 단위로 분할

        Args:
            text: 원본 텍스트

        Returns:
            문장 리스트
        """
        raise NotImplementedError()


def split_sentences(text: str, max_length: int = 200) -> List[str]:
    """
    텍스트를 문장 단위로 분할 (함수형 인터페이스)

    Args:
        text: 원본 텍스트
        max_length: 최대 문장 길이

    Returns:
        문장 리스트
    """
    splitter = SegmentSplitter({'max_length': max_length})
    segments = splitter.split(text)
    return [seg.text for seg in segments]


