import re
from typing import List
from dataclasses import dataclass


# 기존에 사용하던 컨버터나 핸들러는 필요 없으나,
# ParsedSegment 구조체는 pipeline.py와의 호환을 위해 유지합니다.

@dataclass
class ParsedSegment:
    """
    텍스트 파싱 결과 세그먼트 (Plain Text 전용)

    Attributes:
        text: 정제된 텍스트
        pad_silence: 문장 뒤 무음 시간 (기본값 0.0)
        tempo: 재생 속도 (기본값 1.0)
        gain_db: 볼륨 조정 (기본값 0.0)
    """
    text: str
    pad_silence: float = 0.0
    tempo: float = 1.0
    gain_db: float = 0.0


class TextParser:
    """
    Plain Text 파서

    SSML 태그를 무시하거나 제거하고,
    순수 텍스트를 파이프라인 규격에 맞는 세그먼트로 변환합니다.
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        # 기본값 설정
        self.default_tempo = 1.0
        self.default_pad_silence = 0.0
        self.default_gain_db = 0.0

    def parse(self, text: str) -> List[ParsedSegment]:
        """
        입력 텍스트를 세그먼트 리스트로 변환

        Args:
            text: 원본 텍스트

        Returns:
            ParsedSegment 리스트
        """
        if not text:
            return []

        # 1. 태그 제거 (혹시 모를 < > 형태의 텍스트 방어)
        clean_text = self._strip_tags(text)

        # 2. 연속된 공백 정리
        clean_text = " ".join(clean_text.split())

        if not clean_text:
            return []

        # 3. 단일 세그먼트로 반환
        # Plain Text 환경에서는 속성 변화가 없으므로 하나의 덩어리로 넘깁니다.
        # 이후 pipeline.py의 text_token_splitter가 길이에 따라 안전하게 자릅니다.
        return [
            ParsedSegment(
                text=clean_text,
                pad_silence=self.default_pad_silence,
                tempo=self.default_tempo,
                gain_db=self.default_gain_db
            )
        ]

    def _strip_tags(self, text: str) -> str:
        """
        텍스트 내의 모든 XML/HTML 태그 제거
        """
        return re.sub(r'<[^>]+>', '', text).strip()