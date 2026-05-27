"""TB_USER 조회 — 동기 mysql.connector를 asyncio.to_thread로 감쌈."""

import asyncio
import logging
from typing import Any

import mysql.connector

from app.core.config import Config

logger = logging.getLogger(__name__)


def _fetch_user_sync(user_id: str) -> dict[str, Any] | None:
    db_name = Config.AUTH_DB_NAME or Config.DB_NAME
    conn = mysql.connector.connect(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
        database=db_name,
        connection_timeout=Config.DB_CONNECTION_TIMEOUT,
    )
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT USER_ID, USER_PW, USER_NM, USER_AUTH, USE_YN, LOCK_YN, DEPT_ID "
            "FROM TB_USER WHERE USER_ID = %s",
            (user_id,),
        )
        return cursor.fetchone()
    finally:
        conn.close()


async def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(_fetch_user_sync, user_id)
