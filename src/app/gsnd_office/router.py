"""TB_GSND_OFFICE CRUD 라우터 — 어드민 전용."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.gsnd_office.dependencies import get_gsnd_office_service
from app.gsnd_office.schemas import (
    GsndOfficeCreate,
    GsndOfficeListResponse,
    GsndOfficeResponse,
    GsndOfficeUpdate,
)
from app.gsnd_office.service import GsndOfficeService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/gsnd-office", tags=["Admin - 경남 복지관"])

_ServiceDep = Annotated[GsndOfficeService, Depends(get_gsnd_office_service)]


@router.get("", response_model=GsndOfficeListResponse, summary="목록 조회")
async def list_gsnd_office(
    service: _ServiceDep,
    sigungu: str | None = Query(None, description="시군구 필터 (완전 일치)"),
    eupmyeondong: str | None = Query(None, description="읍면동 필터 (완전 일치)"),
    keyword: str | None = Query(None, description="키워드 — 센터명·주소·전화번호 부분 일치"),
    page: int = Query(1, ge=1, description="페이지 번호 (1-based)"),
    page_size: int = Query(20, ge=1, le=100, description="페이지 크기"),
):
    """경남 복지관(사무소) 목록을 페이지네이션으로 반환합니다."""
    return service.list_records(
        sigungu=sigungu,
        eupmyeondong=eupmyeondong,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )


@router.get("/{record_id}", response_model=GsndOfficeResponse, summary="단건 조회")
async def get_gsnd_office(record_id: int, service: _ServiceDep):
    """ID로 경남 복지관 레코드를 조회합니다."""
    return service.get_record(record_id)


@router.post(
    "",
    response_model=GsndOfficeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="생성",
)
async def create_gsnd_office(payload: GsndOfficeCreate, service: _ServiceDep):
    """새 경남 복지관 레코드를 생성합니다."""
    return service.create_record(payload)


@router.patch("/{record_id}", response_model=GsndOfficeResponse, summary="부분 수정")
async def update_gsnd_office(record_id: int, payload: GsndOfficeUpdate, service: _ServiceDep):
    """지정한 필드만 수정합니다 (PATCH). 보내지 않은 필드는 변경되지 않습니다."""
    return service.update_record(record_id, payload)


@router.delete(
    "/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="삭제",
)
async def delete_gsnd_office(record_id: int, service: _ServiceDep):
    """경남 복지관 레코드를 삭제합니다."""
    service.delete_record(record_id)
