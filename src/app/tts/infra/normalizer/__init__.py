"""
텍스트 정규화 서브패키지

텍스트를 TTS 엔진이 올바르게 발음할 수 있도록 정규화합니다.

기존 파일 참조:
- saltlux/text/korean.py (한국어 정규화)
- saltlux/text/lang_normalize.py (영어 정규화)

주요 모듈:
- base: 정규화 추상 인터페이스
- korean: 한국어 텍스트 정규화
- english: 영어 텍스트 정규화
- patterns/: 패턴별 핸들러 (이메일, 전화번호, 날짜, 숫자)
"""

from .base import BaseNormalizer, PatternHandler
from .korean import KoreanNormalizer
from .english import EnglishNormalizer

__all__ = [
    'BaseNormalizer',
    'PatternHandler',
    'KoreanNormalizer',
    'EnglishNormalizer',
]
