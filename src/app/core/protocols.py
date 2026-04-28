"""
도메인 간 계약을 정의하는 Protocol 모음.

- 외부 DI 프레임워크 없이 FastAPI Depends만 사용
- 각 Protocol은 runtime_checkable로 선언해 isinstance 검증 가능
- Mock 교체 시 typing.Protocol만 구현하면 됨 (상속 불필요)
"""

from __future__ import annotations

from typing import Any, AsyncGenerator, Optional, Protocol, runtime_checkable


@runtime_checkable
class LLMClientProtocol(Protocol):
    """LLM HTTP 클라이언트 계약."""

    @property
    def is_closed(self) -> bool: ...

    async def post(self, url: str, **kwargs: Any) -> Any: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class ChatHistoryRepositoryProtocol(Protocol):
    """채팅 히스토리 레포지터리 계약."""

    def upsert_history(
        self,
        user_id: str,
        conv_id: str,
        messages: list,
        overwrite: bool = False,
    ) -> None: ...

    def get_history(self, user_id: str, conv_id: str) -> list: ...

    def delete_history(self, user_id: str, conv_id: str) -> None: ...


@runtime_checkable
class MarinerRetrieverProtocol(Protocol):
    """Mariner 검색엔진 계약."""

    def search_general(
        self,
        query: str,
        collection: str,
        top_n: int = 5,
        **kwargs: Any,
    ) -> list: ...

    def search_welfare_center(
        self,
        keyword: str,
        sigun_filters: list,
        **kwargs: Any,
    ) -> list: ...


@runtime_checkable
class SuggestQuestionsServiceProtocol(Protocol):
    """추천 질문 생성 서비스 계약."""

    async def generate(
        self,
        user_query: str,
        assistant_response: str,
        max_questions: int = 5,
    ) -> list[str]: ...
