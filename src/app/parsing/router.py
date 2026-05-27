"""HWPX 복지서비스 파싱 & DB 적재 라우터."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
import mysql.connector

from app.core.config import get_config
from app.parsing.schemas import HwpxUploadResponse
from app.parsing.service import HwpxParserService

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
