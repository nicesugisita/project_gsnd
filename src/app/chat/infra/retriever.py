"""
MarinerRetriever — Mariner 검색엔진 쿼리셋 래퍼.

JVM이 초기화된 후 lifespan에서 1회 생성하여 app.state.retriever에 저장.
stateless이므로 인스턴스 하나를 재사용한다.
"""

from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)


class MarinerRetriever:
    """
    Mariner 쿼리셋 함수를 하나의 인터페이스로 묶는 래퍼.

    JVM 호출은 동기식이므로 asyncio.to_thread()로 감싸서 사용할 것.
    """

    # ── General 검색 ─────────────────────────────────────────────────────────

    def search_general(
        self,
        query: str,
        collection: str,
        top_n: int = 5,
        **kwargs: Any,
    ) -> List[dict]:
        """GSND / OKMS 통합 general 검색."""
        from app.mariner.queryset_gsnd import query_GSND_general_documents
        try:
            return query_GSND_general_documents(query, top_n=top_n, **kwargs)
        except Exception:
            logger.exception("[Retriever] search_general 실패: query=%s", query[:50])
            return []

    def search_okms(
        self,
        query: str,
        top_n: int = 5,
        **kwargs: Any,
    ) -> List[dict]:
        """OKMS 컬렉션 검색."""
        from app.mariner.queryset_okms import query_group_a_documents
        try:
            return query_group_a_documents(query, top_n=top_n, **kwargs)
        except Exception:
            logger.exception("[Retriever] search_okms 실패: query=%s", query[:50])
            return []

    # ── Welfare 검색 ──────────────────────────────────────────────────────────

    def search_welfare_center(
        self,
        keyword: str,
        sigun_filters: list,
        **kwargs: Any,
    ) -> List[dict]:
        """복지시설 컬렉션 검색."""
        from app.mariner.queryset_welfare import query_welfare_center_documents
        try:
            kw = {k: v for k, v in kwargs.items() if k not in {"excluded_chunk_ids"}}
            return query_welfare_center_documents(
                keyword, sigun_filters=sigun_filters, **kw
            )
        except Exception:
            logger.exception("[Retriever] search_welfare_center 실패: keyword=%s", keyword[:50])
            return []

    def search_welfare_tel(
        self,
        keyword: str,
        sigun_filters: list,
        **kwargs: Any,
    ) -> List[dict]:
        """복지 문의처 컬렉션 검색."""
        from app.mariner.queryset_welfare_tel import query_welfare_tel_documents
        try:
            kw = {k: v for k, v in kwargs.items() if k != "excluded_chunk_ids"}
            return query_welfare_tel_documents(
                keyword, sigun_filters=sigun_filters, **kw
            )
        except Exception:
            logger.exception("[Retriever] search_welfare_tel 실패: keyword=%s", keyword[:50])
            return []
