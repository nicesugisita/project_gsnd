"""office 도메인 FastAPI 의존성."""

from __future__ import annotations

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
import mysql.connector

from app.core.config import get_config
from app.office.repository import OfficeRepository
from app.office.service import OfficeService


def get_okms2_db() -> Generator:
    """okms2 DB 커넥션 — 요청 단위로 생성/반환."""
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
        yield conn
    finally:
        conn.close()


def get_office_service(
    conn: Annotated[object, Depends(get_okms2_db)],
) -> OfficeService:
    return OfficeService(OfficeRepository(conn))
