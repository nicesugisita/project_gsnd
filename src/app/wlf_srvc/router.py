"""복지서비스 CRUD 라우터."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
import mysql.connector

from app.core.config import get_config
from app.wlf_srvc.repository import WlfSrvcRepository
from app.wlf_srvc.schemas import WlfSrvcListResponse, WlfSrvcResponse, WlfSrvcUpdate
from app.wlf_srvc.service import WlfSrvcService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/wlf-srvc", tags=["Admin - 복지서비스"])


def _get_service():
    cfg = get_config()
    conn = mysql.connector.connect(
        host=cfg.DB_HOST,
        user=cfg.DB_USER,
        password=cfg.DB_PASSWORD,
        database=cfg.OKMS2_DB_NAME,
        port=cfg.DB_PORT,
        autocommit=True,
        connection_timeout=cfg.DB_CONNECTION_TIMEOUT,
    )
    try:
        yield WlfSrvcService(WlfSrvcRepository(conn, table=cfg.HWPX_SOURCE_UUID_TABLE))
    finally:
        conn.close()


_SvcDep = Annotated[WlfSrvcService, Depends(_get_service)]


@router.get("", response_model=WlfSrvcListResponse, summary="목록 조회")
async def list_wlf_srvc(
    service: _SvcDep,
    wlf_yr: str | None = Query(None, description="복지연도 필터"),
    sigun_cd: str | None = Query(None, description="시군코드 필터"),
    use_yn: str | None = Query(None, description="사용여부 필터 (Y/N)"),
    keyword: str | None = Query(None, description="서비스명·파일명·담당부서 키워드"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> WlfSrvcListResponse:
    return service.list_records(
        wlf_yr=wlf_yr,
        sigun_cd=sigun_cd,
        use_yn=use_yn,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )


@router.get("/{record_id}", response_model=WlfSrvcResponse, summary="단건 조회")
async def get_wlf_srvc(record_id: int, service: _SvcDep) -> WlfSrvcResponse:
    return service.get_record(record_id)


@router.patch("/{record_id}", response_model=WlfSrvcResponse, summary="부분 수정")
async def update_wlf_srvc(
    record_id: int, payload: WlfSrvcUpdate, service: _SvcDep
) -> WlfSrvcResponse:
    return service.update_record(record_id, payload)


@router.delete("/{record_id}", status_code=status.HTTP_204_NO_CONTENT, summary="삭제")
async def delete_wlf_srvc(record_id: int, service: _SvcDep):
    service.delete_record(record_id)
