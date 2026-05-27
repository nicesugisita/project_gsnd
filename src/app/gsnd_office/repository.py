"""TB_GSND_OFFICE 테이블 CRUD 레포지터리 (okms2 DB)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TABLE = "TB_GSND_OFFICE"

_SELECT_COLS = (
    "`id`, "
    "`SIGUNGU` AS sigungu, "
    "`CENTER_NM` AS center_nm, "
    "`EUPMYEONDONG` AS eupmyeondong, "
    "`TEL_NO` AS tel_no, "
    "`ADDRESS` AS address"
)

_WRITE_COL_MAP: dict[str, str] = {
    "id": "id",
    "sigungu": "SIGUNGU",
    "center_nm": "CENTER_NM",
    "eupmyeondong": "EUPMYEONDONG",
    "tel_no": "TEL_NO",
    "address": "ADDRESS",
}


class GsndOfficeRepository:
    """주입받은 DB 커넥션으로 TB_GSND_OFFICE를 CRUD."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def list(
        self,
        *,
        sigungu: str | None = None,
        eupmyeondong: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int]:
        """목록 조회. (rows, total) 반환."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if sigungu:
            where_clauses.append("`SIGUNGU` = %s")
            params.append(sigungu)
        if eupmyeondong:
            where_clauses.append("`EUPMYEONDONG` = %s")
            params.append(eupmyeondong)
        if keyword:
            where_clauses.append("(`CENTER_NM` LIKE %s OR `ADDRESS` LIKE %s OR `TEL_NO` LIKE %s)")
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
                f"ORDER BY `id` DESC LIMIT %s OFFSET %s",
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
                f"SELECT {_SELECT_COLS} FROM `{_TABLE}` WHERE `id` = %s",
                (record_id,),
            )
            return cursor.fetchone()
        finally:
            cursor.close()

    def create(self, data: dict) -> int:
        """레코드 생성. 신규 id 반환 (lastrowid 또는 명시 id)."""
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
            if cursor.lastrowid:
                return cursor.lastrowid
            if "id" in data and data["id"] is not None:
                return int(data["id"])
            raise ValueError("INSERT 후 id를 확인할 수 없습니다.")
        finally:
            cursor.close()

    def update(self, record_id: int, data: dict) -> bool:
        """지정 필드 업데이트. 변경된 행이 있으면 True."""
        set_clauses: list[str] = []
        values: list[Any] = []

        for field, col in _WRITE_COL_MAP.items():
            if field == "id":
                continue
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
                f"UPDATE `{_TABLE}` SET {set_sql} WHERE `id` = %s",
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
                f"DELETE FROM `{_TABLE}` WHERE `id` = %s",
                (record_id,),
            )
            return cursor.rowcount > 0
        finally:
            cursor.close()
