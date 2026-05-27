"""TB_OFFICE CRUD 라우터 — 어드민 전용."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.office.dependencies import get_office_service
from app.office.schemas import (
    OfficeCreate,
    OfficeListResponse,
    OfficeResponse,
    OfficeUpdate,
)
from app.office.service import OfficeService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/office", tags=["Admin - 시설 정보"])

_ServiceDep = Annotated[OfficeService, Depends(get_office_service)]


@router.get("", response_model=OfficeListResponse, summary="목록 조회")
async def list_office(
    service: _ServiceDep,
    sigun: str | None = Query(None, description="시군 필터 (완전 일치)"),
    facility_type: str | None = Query(None, description="시설종류 필터 (완전 일치)"),
    facility_category: str | None = Query(None, description="시설분류 필터 (완전 일치)"),
    category_large: str | None = Query(None, description="대분류 필터 (완전 일치)"),
    keyword: str | None = Query(
        None, description="키워드 — 시설명·소재지·도로명주소·전화번호 부분 일치"
    ),
    page: int = Query(1, ge=1, description="페이지 번호 (1-based)"),
    page_size: int = Query(20, ge=1, le=100, description="페이지 크기"),
):
    """시설 정보 목록을 페이지네이션으로 반환합니다."""
    return service.list_records(
        sigun=sigun,
        facility_type=facility_type,
        facility_category=facility_category,
        category_large=category_large,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )


@router.get("/{record_id}", response_model=OfficeResponse, summary="단건 조회")
async def get_office(record_id: int, service: _ServiceDep):
    """ID로 시설 레코드를 조회합니다."""
    return service.get_record(record_id)


@router.post(
    "",
    response_model=OfficeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="생성",
)
async def create_office(payload: OfficeCreate, service: _ServiceDep):
    """새 시설 레코드를 생성합니다."""
    return service.create_record(payload)


@router.patch("/{record_id}", response_model=OfficeResponse, summary="부분 수정")
async def update_office(record_id: int, payload: OfficeUpdate, service: _ServiceDep):
    """지정한 필드만 수정합니다 (PATCH). 보내지 않은 필드는 변경되지 않습니다."""
    return service.update_record(record_id, payload)


@router.delete(
    "/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="삭제",
)
async def delete_office(record_id: int, service: _ServiceDep):
    """시설 레코드를 삭제합니다."""
    service.delete_record(record_id)
