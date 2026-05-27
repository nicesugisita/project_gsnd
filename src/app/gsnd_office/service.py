"""TB_GSND_OFFICE 비즈니스 로직."""

from __future__ import annotations

import logging

from fastapi import HTTPException, status

from app.gsnd_office.repository import GsndOfficeRepository
from app.gsnd_office.schemas import (
    GsndOfficeCreate,
    GsndOfficeListResponse,
    GsndOfficeResponse,
    GsndOfficeUpdate,
)

logger = logging.getLogger(__name__)


class GsndOfficeService:
    def __init__(self, repo: GsndOfficeRepository) -> None:
        self._repo = repo

    def list_records(
        self,
        *,
        sigungu: str | None,
        eupmyeondong: str | None,
        keyword: str | None,
        page: int,
        page_size: int,
    ) -> GsndOfficeListResponse:
        rows, total = self._repo.list(
            sigungu=sigungu,
            eupmyeondong=eupmyeondong,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
        return GsndOfficeListResponse(
            total=total,
            page=page,
            page_size=page_size,
            items=[GsndOfficeResponse(**row) for row in rows],
        )

    def get_record(self, record_id: int) -> GsndOfficeResponse:
        row = self._repo.get(record_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"레코드를 찾을 수 없습니다. (id={record_id})",
            )
        return GsndOfficeResponse(**row)

    def create_record(self, payload: GsndOfficeCreate) -> GsndOfficeResponse:
        new_id = self._repo.create(payload.model_dump(exclude_none=True))
        return self.get_record(new_id)

    def update_record(self, record_id: int, payload: GsndOfficeUpdate) -> GsndOfficeResponse:
        self.get_record(record_id)
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="수정할 필드가 하나 이상 있어야 합니다.",
            )
        self._repo.update(record_id, data)
        return self.get_record(record_id)

    def delete_record(self, record_id: int) -> None:
        self.get_record(record_id)
        self._repo.delete(record_id)
