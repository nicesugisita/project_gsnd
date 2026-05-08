"""
영어 텍스트 정규화

영어 텍스트를 TTS 엔진이 올바르게 발음할 수 있도록 변환합니다.

기존 코드 참조: saltlux/text/lang_normalize.py
- en_normalize() 함수

외부 라이브러리 사용:
- libs/tn/english/normalizer (Text Normalization)
- libs/inflect (숫자 -> 영어 변환)
"""

from .base import BaseNormalizer


class EnglishNormalizer(BaseNormalizer):
    """
    영어 텍스트 정규화기

    숫자, 날짜, 약어 등을 영어 발화용으로 변환합니다.
    """

    def __init__(self, config: dict = None):
        """
        Args:
            config: 정규화 설정
        """
        super().__init__(config)
        self._inflect_engine = None
        self._tn_normalizer = None

    def _init_inflect(self):
        """inflect 엔진 초기화 (지연 로딩)"""
        if self._inflect_engine is None:
            try:
                from libs.inflect import engine
                self._inflect_engine = engine()
            except ImportError:
                pass

    def _init_tn(self):
        """Text Normalization 초기화 (지연 로딩)"""
        if self._tn_normalizer is None:
            try:
                from libs.tn.english.normalizer import Normalizer
                self._tn_normalizer = Normalizer()
            except ImportError:
                pass

    def normalize(self, text: str) -> str:
        """
        영어 텍스트 정규화 실행

        Args:
            text: 원본 텍스트

        Returns:
            정규화된 텍스트
        """
        raise NotImplementedError()
