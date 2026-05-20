"""TB_OFFICE 테이블 CRUD 레포지터리 (okms2 DB)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TABLE = "TB_OFFICE"

_SELECT_COLS = (
    "`id`, "
    "`시군` AS sigun, "
    "`시설명` AS facility_name, "
    "`경위도X좌표` AS coord_x, "
    "`경위도Y좌표` AS coord_y, "
    "`정제도로명주소` AS refined_road_address, "
    "`정제지번주소` AS refined_lot_address, "
    "`소재지` AS location, "
    "`시설종류` AS facility_type, "
    "`시설분류` AS facility_category, "
    "`정원` AS capacity, "
    "`대분류` AS category_large, "
    "`소분류` AS category_small, "
    "`대분류2` AS category_large2, "
    "`소분류2` AS category_small2, "
    "`대분류3` AS category_large3, "
    "`소분류3` AS category_small3, "
    "`homepage`, `tel`, `created_at`, `updated_at`"
)

_WRITE_COL_MAP: dict[str, str] = {
    "sigun": "시군",
    "facility_name": "시설명",
    "coord_x": "경위도X좌표",
    "coord_y": "경위도Y좌표",
    "refined_road_address": "정제도로명주소",
    "refined_lot_address": "정제지번주소",
    "location": "소재지",
    "facility_type": "시설종류",
    "facility_category": "시설분류",
    "capacity": "정원",
    "category_large": "대분류",
    "category_small": "소분류",
    "category_large2": "대분류2",
    "category_small2": "소분류2",
    "category_large3": "대분류3",
    "category_small3": "소분류3",
    "homepage": "homepage",
    "tel": "tel",
}


class OfficeRepository:
    """주입받은 DB 커넥션으로 TB_OFFICE를 CRUD."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def list(
        self,
        *,
        sigun: str | None = None,
        facility_type: str | None = None,
        facility_category: str | None = None,
        category_large: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int]:
        """목록 조회. (rows, total) 반환."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if sigun:
            where_clauses.append("`시군` = %s")
            params.append(sigun)
        if facility_type:
            where_clauses.append("`시설종류` = %s")
            params.append(facility_type)
        if facility_category:
            where_clauses.append("`시설분류` = %s")
            params.append(facility_category)
        if category_large:
            where_clauses.append("`대분류` = %s")
            params.append(category_large)
        if keyword:
            where_clauses.append(
                "(`시설명` LIKE %s OR `소재지` LIKE %s OR `정제도로명주소` LIKE %s OR `tel` LIKE %s)"
            )
            like = f"%{keyword}%"
            params.extend([like, like, like, like])

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
