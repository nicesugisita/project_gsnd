"""
정규화 기본 인터페이스

모든 정규화 클래스와 패턴 핸들러가 구현해야 하는 추상 인터페이스.
"""

from abc import ABC, abstractmethod
from typing import List


class PatternHandler(ABC):
    """
    패턴 처리 기본 인터페이스

    특정 패턴(이메일, 전화번호, 날짜 등)을 감지하고
    발화용 텍스트로 변환합니다.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """핸들러 이름"""
        pass

    @property
    @abstractmethod
    def priority(self) -> int:
        """처리 우선순위 (낮을수록 먼저 실행)"""
        pass

    @abstractmethod
    def match(self, text: str) -> bool:
        """
        텍스트가 이 패턴과 일치하는지 확인

        Args:
            text: 확인할 텍스트

        Returns:
            패턴 일치 여부
        """
        pass

    @abstractmethod
    def normalize(self, text: str) -> str:
        """
        패턴을 정규화된 텍스트로 변환

        Args:
            text: 원본 텍스트

        Returns:
            정규화된 텍스트
        """
        pass

    def process(self, text: str) -> str:
        """
        텍스트 처리 (매칭 + 변환)

        match()가 True인 부분만 normalize()를 적용합니다.

        Args:
            text: 원본 텍스트

        Returns:
            처리된 텍스트
        """
        # 기본 구현: 서브클래스에서 오버라이드 가능
        return self.normalize(text)


class BaseNormalizer(ABC):
    """
    텍스트 정규화 기본 클래스

    여러 PatternHandler를 조합하여 텍스트를 정규화합니다.
    """

    def __init__(self, config: dict = None):
        """
        Args:
            config: 정규화 설정
        """
        self.config = config or {}
        self.handlers: List[PatternHandler] = []

    def register_handler(self, handler: PatternHandler) -> None:
        """
        패턴 핸들러 등록

        Args:
            handler: 등록할 핸들러
        """
        self.handlers.append(handler)
        # 우선순위에 따라 정렬
        self.handlers.sort(key=lambda h: h.priority)

    @abstractmethod
    def normalize(self, text: str) -> str:
        """
        텍스트 정규화 실행

        Args:
            text: 원본 텍스트

        Returns:
            정규화된 텍스트
        """
        pass

    def _apply_handlers(self, text: str) -> str:
        """
        등록된 핸들러를 순차적으로 적용

        Args:
            text: 원본 텍스트

        Returns:
            처리된 텍스트
        """
        result = text
        for handler in self.handlers:
            result = handler.process(result)
        return result
