"""HWPX 복지서비스 파싱 & DB 적재 라우터."""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
import mysql.connector

from app.core.config import get_config
from app.parsing.schemas import (
    DeleteTempRowsResponse,
    HwpxUploadResponse,
    PublishRequest,
    PublishResponse,
    TempRowUpdate,
)
from app.parsing.service import HwpxParserService
from app.wlf_srvc.schemas import WlfSrvcListResponse, WlfSrvcResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/parsing", tags=["Admin - HWPX 파싱"])

_MAX_FILE_SIZE_MB = 50
_MAX_FILE_SIZE_BYTES = _MAX_FILE_SIZE_MB * 1024 * 1024


def _get_conn():
    """okms2 DB 커넥션 — 트랜잭션 지원 (autocommit=False)."""
    cfg = get_config()
    conn = mysql.connector.connect(
        host=cfg.DB_HOST,
        user=cfg.DB_USER,
        password=cfg.DB_PASSWORD,
        database=cfg.OKMS2_DB_NAME,
        port=cfg.DB_PORT,
        autocommit=False,
        connection_timeout=cfg.DB_CONNECTION_TIMEOUT,
    )
    try:
        yield conn
    finally:
        conn.close()


_ConnDep = Annotated[object, Depends(_get_conn)]


def _resolve_table(cfg, table_key: Literal["temp", "real"]) -> str:
    return cfg.HWPX_TARGET_TABLE if table_key == "temp" else cfg.HWPX_SOURCE_UUID_TABLE


@router.get(
    "/rows",
    response_model=WlfSrvcListResponse,
    status_code=status.HTTP_200_OK,
    summary="파싱 테이블 데이터 목록 조회",
)
async def list_rows(
    conn: _ConnDep,
    table_key: Literal["temp", "real"] = Query("temp", description="조회 테이블: temp=임시, real=실 테이블"),
    wlf_yr: str | None = Query(None),
    sigun_cd: str | None = Query(None),
    keyword: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> WlfSrvcListResponse:
    cfg = get_config()
    service = HwpxParserService(conn)
    return service.list_temp_rows(
        temp_table=_resolve_table(cfg, table_key),
        wlf_yr=wlf_yr,
        sigun_cd=sigun_cd,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )


@router.patch(
    "/rows/{sn}",
    response_model=WlfSrvcResponse,
    status_code=status.HTTP_200_OK,
    summary="파싱 테이블 행 수정",
)
async def update_row(
    sn: int,
    body: TempRowUpdate,
    conn: _ConnDep,
    table_key: Literal["temp", "real"] = Query("temp"),
) -> WlfSrvcResponse:
    cfg = get_config()
    service = HwpxParserService(conn)
    result = service.update_temp_row(
        temp_table=_resolve_table(cfg, table_key),
        sn=sn,
        data=body.model_dump(exclude_unset=True),
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SN={sn} 행을 찾을 수 없습니다.",
        )
    return result


@router.delete(
    "/rows/{sn}",
    response_model=DeleteTempRowsResponse,
    status_code=status.HTTP_200_OK,
    summary="파싱 테이블 행 단건 삭제",
)
async def delete_row(
    sn: int,
    conn: _ConnDep,
    table_key: Literal["temp", "real"] = Query("temp"),
) -> DeleteTempRowsResponse:
    cfg = get_config()
    service = HwpxParserService(conn)
    table = _resolve_table(cfg, table_key)
    deleted = service.delete_row(temp_table=table, sn=sn)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"SN={sn} 행을 찾을 수 없습니다.")
    return DeleteTempRowsResponse(table=table, deleted_count=1)


@router.delete(
    "/rows",
    response_model=DeleteTempRowsResponse,
    status_code=status.HTTP_200_OK,
    summary="파싱 테이블 행 일괄 삭제",
    description="wlf_yr 지정 시 해당 연도만, 미지정 시 전체 삭제.",
)
async def delete_rows(
    conn: _ConnDep,
    table_key: Literal["temp", "real"] = Query("temp"),
    wlf_yr: str | None = Query(None, description="삭제할 복지연도. 미지정 시 전체."),
) -> DeleteTempRowsResponse:
    cfg = get_config()
    service = HwpxParserService(conn)
    table = _resolve_table(cfg, table_key)
    count = service.delete_rows(temp_table=table, wlf_yr=wlf_yr)
    return DeleteTempRowsResponse(table=table, deleted_count=count)


@router.post(
    "/hwpx",
    response_model=HwpxUploadResponse,
    status_code=status.HTTP_200_OK,
    summary="HWPX 파일 파싱 및 DB 적재",
    description=(
        "하나 이상의 .hwpx 파일을 업로드하면 파싱해 DB에 INSERT합니다.\n\n"
        "적재 대상 테이블·연도·지역·FILE_DIR 등 설정은 서버 `.env`(HWPX_*)로 관리합니다."
    ),
)
async def upload_hwpx(
    conn: _ConnDep,
    files: list[UploadFile],
) -> HwpxUploadResponse:
    """HWPX 파일들을 파싱해 복지서비스 DB 테이블에 적재한다."""
    cfg = get_config()

    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="파일이 없습니다.")

    hwpx_files = [f for f in files if (f.filename or "").lower().endswith(".hwpx")]
    if not hwpx_files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'.hwpx' 확장자 파일만 지원합니다.",
        )

    file_contents: list[tuple[str, bytes]] = []
    for upload in hwpx_files:
        raw = await upload.read()
        if len(raw) > _MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"{upload.filename}: 파일 크기 초과 (최대 {_MAX_FILE_SIZE_MB}MB)",
            )
        file_contents.append((upload.filename or "upload.hwpx", raw))

    service = HwpxParserService(conn)
    result = service.parse_and_insert(
        file_contents=file_contents,
        target_table=cfg.HWPX_TARGET_TABLE,
        welfare_year=cfg.HWPX_WELFARE_YEAR or None,
        region=cfg.HWPX_REGION or None,
        file_dir=cfg.HWPX_FILE_DIR,
        delete_existing=cfg.HWPX_DELETE_EXISTING,
        source_uuid_table=cfg.HWPX_SOURCE_UUID_TABLE,
    )

    if result.errors and result.inserted_count == 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"errors": result.errors},
        )

    return result


@router.post(
    "/publish",
    response_model=PublishResponse,
    status_code=status.HTTP_200_OK,
    summary="temp 테이블 → 실 테이블 반영",
    description=(
        "temp 테이블의 데이터를 실 테이블로 반영합니다.\n\n"
        "- `year` 지정 시 해당 연도만 반영합니다.\n"
        "- `org_names` 지정 시 해당 ORG_NM만 반영합니다.\n"
        "- 둘 다 None이면 temp 테이블 전체를 반영합니다.\n\n"
        "반영 대상 테이블은 서버 `.env`의 `HWPX_TARGET_TABLE`(temp)과 `HWPX_SOURCE_UUID_TABLE`(실 테이블)로 결정됩니다."
    ),
)
async def publish_hwpx(
    conn: _ConnDep,
    body: PublishRequest,
) -> PublishResponse:
    """temp 테이블 데이터를 실 테이블에 반영한다."""
    cfg = get_config()

    source_table = cfg.HWPX_TARGET_TABLE
    target_table = cfg.HWPX_SOURCE_UUID_TABLE

    if source_table == target_table:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"source({source_table})와 target({target_table})이 동일합니다. "
                ".env의 HWPX_TARGET_TABLE(temp)과 HWPX_SOURCE_UUID_TABLE(실 테이블)을 확인하세요."
            ),
        )

    service = HwpxParserService(conn)
    try:
        return service.publish_to_real_table(
            source_table=source_table,
            target_table=target_table,
            year=body.year,
            org_names=body.org_names,
        )
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        ) from e
