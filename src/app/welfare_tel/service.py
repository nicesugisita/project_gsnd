"""WELFARE_TEL 비즈니스 로직."""

from __future__ import annotations

import logging

from fastapi import HTTPException, status

from app.welfare_tel.repository import WelfareTelRepository
from app.welfare_tel.schemas import (
    WelfareTelCreate,
    WelfareTelListResponse,
    WelfareTelResponse,
    WelfareTelUpdate,
)

logger = logging.getLogger(__name__)


class WelfareTelService:
    def __init__(self, repo: WelfareTelRepository) -> None:
        self._repo = repo

    def list_records(
        self,
        *,
        region: str | None,
        year: str | None,
        keyword: str | None,
        page: int,
        page_size: int,
    ) -> WelfareTelListResponse:
        rows, total = self._repo.list(
            region=region,
            year=year,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
        return WelfareTelListResponse(
            total=total,
            page=page,
            page_size=page_size,
            items=[WelfareTelResponse(**row) for row in rows],
        )

    def get_record(self, record_id: int) -> WelfareTelResponse:
        row = self._repo.get(record_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"레코드를 찾을 수 없습니다. (id={record_id})",
            )
        return WelfareTelResponse(**row)

    def create_record(self, payload: WelfareTelCreate) -> WelfareTelResponse:
        new_id = self._repo.create(payload.model_dump(exclude_none=True))
        return self.get_record(new_id)

    def update_record(self, record_id: int, payload: WelfareTelUpdate) -> WelfareTelResponse:
        self.get_record(record_id)  # 존재 여부 확인 (없으면 404)
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="수정할 필드가 하나 이상 있어야 합니다.",
            )
        self._repo.update(record_id, data)
        return self.get_record(record_id)

    def delete_record(self, record_id: int) -> None:
        self.get_record(record_id)  # 존재 여부 확인 (없으면 404)
        self._repo.delete(record_id)
