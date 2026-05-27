"""TB_OFFICE 비즈니스 로직."""

from __future__ import annotations

import logging

from fastapi import HTTPException, status

from app.office.repository import OfficeRepository
from app.office.schemas import (
    OfficeCreate,
    OfficeListResponse,
    OfficeResponse,
    OfficeUpdate,
)

logger = logging.getLogger(__name__)


class OfficeService:
    def __init__(self, repo: OfficeRepository) -> None:
        self._repo = repo

    def list_records(
        self,
        *,
        sigun: str | None,
        facility_type: str | None,
        facility_category: str | None,
        category_large: str | None,
        keyword: str | None,
        page: int,
        page_size: int,
    ) -> OfficeListResponse:
        rows, total = self._repo.list(
            sigun=sigun,
            facility_type=facility_type,
            facility_category=facility_category,
            category_large=category_large,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
        return OfficeListResponse(
            total=total,
            page=page,
            page_size=page_size,
            items=[OfficeResponse(**row) for row in rows],
        )

    def get_record(self, record_id: int) -> OfficeResponse:
        row = self._repo.get(record_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"레코드를 찾을 수 없습니다. (id={record_id})",
            )
        return OfficeResponse(**row)

    def create_record(self, payload: OfficeCreate) -> OfficeResponse:
        new_id = self._repo.create(payload.model_dump(exclude_none=True))
        return self.get_record(new_id)

    def update_record(self, record_id: int, payload: OfficeUpdate) -> OfficeResponse:
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
