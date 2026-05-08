"""MySQL 연결 팩토리 — 인프라 레이어 전용."""

import mysql.connector

from app.core.config import Config


def get_db_connection():
    """MySQL 커넥션을 생성하고 반환한다. 호출자가 close() 책임을 진다."""
    return mysql.connector.connect(
        host=Config.DB_HOST,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
        database=Config.DB_NAME,
        port=Config.DB_PORT,
        autocommit=True,
        connection_timeout=Config.DB_CONNECTION_TIMEOUT,
    )
