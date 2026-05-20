"""복지서비스 CRUD 비즈니스 로직."""

from __future__ import annotations

import logging

from fastapi import HTTPException, status

from app.wlf_srvc.repository import WlfSrvcRepository
from app.wlf_srvc.schemas import WlfSrvcListResponse, WlfSrvcResponse, WlfSrvcUpdate

logger = logging.getLogger(__name__)


class WlfSrvcService:
    def __init__(self, repo: WlfSrvcRepository) -> None:
        self._repo = repo

    def list_records(
        self,
        *,
        wlf_yr: str | None,
        sigun_cd: str | None,
        use_yn: str | None,
        keyword: str | None,
        page: int,
        page_size: int,
    ) -> WlfSrvcListResponse:
        rows, total = self._repo.list(
            wlf_yr=wlf_yr,
            sigun_cd=sigun_cd,
            use_yn=use_yn,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
        return WlfSrvcListResponse(
            total=total,
            page=page,
            page_size=page_size,
            items=[WlfSrvcResponse(**row) for row in rows],
        )

    def get_record(self, record_id: int) -> WlfSrvcResponse:
        row = self._repo.get(record_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"레코드를 찾을 수 없습니다. (id={record_id})",
            )
        return WlfSrvcResponse(**row)

    def update_record(self, record_id: int, payload: WlfSrvcUpdate) -> WlfSrvcResponse:
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
