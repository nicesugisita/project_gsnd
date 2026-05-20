"""WELFARE_TEL 테이블 CRUD 레포지터리 (okms2 DB)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TABLE = "WELFARE_TEL"

# SELECT 시 Python 필드명으로 alias
_SELECT_COLS = (
    "`순번_NEW` AS id, "
    "`기존_순번` AS old_id, "
    "`지역` AS region, "
    "`사업명` AS project_name, "
    "`문서명` AS doc_name, "
    "`문의처 및 담당부서` AS contact_dept, "
    "`연락처` AS phone, "
    "`담당업무` AS duties, "
    "`연도` AS year, "
    "`created_at`, `updated_at`"
)

# Python 필드명 → DB 컬럼명 매핑 (쓰기 전용)
_WRITE_COL_MAP: dict[str, str] = {
    "old_id": "기존_순번",
    "region": "지역",
    "project_name": "사업명",
    "doc_name": "문서명",
    "contact_dept": "문의처 및 담당부서",
    "phone": "연락처",
    "duties": "담당업무",
    "year": "연도",
}


class WelfareTelRepository:
    """주입받은 DB 커넥션으로 WELFARE_TEL을 CRUD."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def list(
        self,
        *,
        region: str | None = None,
        year: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int]:
        """목록 조회. (rows, total) 반환."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if region:
            where_clauses.append("`지역` = %s")
            params.append(region)
        if year:
            where_clauses.append("`연도` = %s")
            params.append(year)
        if keyword:
            where_clauses.append(
                "(`사업명` LIKE %s OR `담당업무` LIKE %s OR `문의처 및 담당부서` LIKE %s)"
            )
            like = f"%{keyword}%"
            params.extend([like, like, like])

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        offset = (page - 1) * page_size

        cursor = self._conn.cursor(dictionary=True)
        try:
            cursor.execute(f"SELECT COUNT(*) AS cnt FROM `{_TABLE}` {where_sql}", params)
            total: int = cursor.fetchone()["cnt"]

            cursor.execute(
                f"SELECT {_SELECT_COLS} FROM `{_TABLE}` {where_sql} "
                f"ORDER BY `순번_NEW` DESC LIMIT %s OFFSET %s",
                [*params, page_size, offset],
            )
            rows = list(cursor.fetchall())
        finally:
            cursor.close()

        return rows, total

    def get(self, record_id: int) -> dict | None:
        """단건 조회. 없으면 None."""
        cursor = self._conn.cursor(dictionary=True)
        try:
            cursor.execute(
                f"SELECT {_SELECT_COLS} FROM `{_TABLE}` WHERE `순번_NEW` = %s",
                (record_id,),
            )
            return cursor.fetchone()
        finally:
            cursor.close()

    def create(self, data: dict) -> int:
        """레코드 생성. 신규 AUTO_INCREMENT id 반환."""
        col_names: list[str] = []
        values: list[Any] = []

        for field, col in _WRITE_COL_MAP.items():
            if field in data and data[field] is not None:
                col_names.append(f"`{col}`")
                values.append(data[field])

        col_sql = ", ".join(col_names)
        placeholder_sql = ", ".join(["%s"] * len(values))

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                f"INSERT INTO `{_TABLE}` ({col_sql}) VALUES ({placeholder_sql})",
                values,
            )
            return cursor.lastrowid
        finally:
            cursor.close()

    def update(self, record_id: int, data: dict) -> bool:
        """지정 필드 업데이트. 변경된 행이 있으면 True."""
        set_clauses: list[str] = []
        values: list[Any] = []

        for field, col in _WRITE_COL_MAP.items():
            if field in data:
                set_clauses.append(f"`{col}` = %s")
                values.append(data[field])

        if not set_clauses:
            return False

        values.append(record_id)
        set_sql = ", ".join(set_clauses)

        cursor = self._conn.cursor()
        try:
            cursor.execute(
                f"UPDATE `{_TABLE}` SET {set_sql} WHERE `순번_NEW` = %s",
                values,
            )
            return cursor.rowcount > 0
        finally:
            cursor.close()

    def delete(self, record_id: int) -> bool:
        """레코드 삭제. 삭제된 행이 있으면 True."""
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                f"DELETE FROM `{_TABLE}` WHERE `순번_NEW` = %s",
                (record_id,),
            )
            return cursor.rowcount > 0
        finally:
            cursor.close()
