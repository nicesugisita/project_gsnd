"""WELFARE_TEL CRUD 라우터 — 어드민 전용."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.welfare_tel.dependencies import get_welfare_tel_service
from app.welfare_tel.schemas import (
    WelfareTelCreate,
    WelfareTelListResponse,
    WelfareTelResponse,
    WelfareTelUpdate,
)
from app.welfare_tel.service import WelfareTelService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/welfare-tel", tags=["Admin - 복지 연락처"])

_ServiceDep = Annotated[WelfareTelService, Depends(get_welfare_tel_service)]


@router.get("", response_model=WelfareTelListResponse, summary="목록 조회")
async def list_welfare_tel(
    service: _ServiceDep,
    region: str | None = Query(None, description="지역 필터 (완전 일치)"),
    year: str | None = Query(None, description="연도 필터 (완전 일치)"),
    keyword: str | None = Query(None, description="키워드 — 사업명·담당업무·문의처 부분 일치"),
    page: int = Query(1, ge=1, description="페이지 번호 (1-based)"),
    page_size: int = Query(20, ge=1, le=100, description="페이지 크기"),
):
    """복지 연락처 목록을 페이지네이션으로 반환합니다."""
    return service.list_records(
        region=region,
        year=year,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )


@router.get("/{record_id}", response_model=WelfareTelResponse, summary="단건 조회")
async def get_welfare_tel(record_id: int, service: _ServiceDep):
    """순번(순번_NEW)으로 복지 연락처를 조회합니다."""
    return service.get_record(record_id)


@router.post(
    "",
    response_model=WelfareTelResponse,
    status_code=status.HTTP_201_CREATED,
    summary="생성",
)
async def create_welfare_tel(payload: WelfareTelCreate, service: _ServiceDep):
    """새 복지 연락처 레코드를 생성합니다."""
    return service.create_record(payload)


@router.patch("/{record_id}", response_model=WelfareTelResponse, summary="부분 수정")
async def update_welfare_tel(
    record_id: int, payload: WelfareTelUpdate, service: _ServiceDep
):
    """지정한 필드만 수정합니다 (PATCH). 보내지 않은 필드는 변경되지 않습니다."""
    return service.update_record(record_id, payload)


@router.delete(
    "/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="삭제",
)
async def delete_welfare_tel(record_id: int, service: _ServiceDep):
    """복지 연락처 레코드를 삭제합니다."""
    service.delete_record(record_id)
