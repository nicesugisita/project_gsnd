"""tbl_wlf_srvc CRUD 레포지터리."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_SELECT_COLS = (
    "WLF_SRVC_SN AS id, "
    "WLF_SRVC_APLY_FORM_SN AS wlf_srvc_aply_form_sn, "
    "WLF_YR AS wlf_yr, "
    "WLF_SRVC_NM AS wlf_srvc_nm, "
    "LFTM_CYCL_CD AS lftm_cycl_cd, "
    "HSHD_STTN_CD AS hshd_sttn_cd, "
    "SIGUN_CD AS sigun_cd, "
    "IMG_ORGNL_FILE_NM AS img_orgnl_file_nm, "
    "IMG_FILE_NM AS img_file_nm, "
    "IMG_FILE_PATH_NM AS img_file_path_nm, "
    "WLF_SRVC_CN AS wlf_srvc_cn, "
    "APLY_YN AS aply_yn, "
    "APLY_BGNG_DT AS aply_bgng_dt, "
    "APLY_END_DT AS aply_end_dt, "
    "APLY_PRD_TYPE AS aply_prd_type, "
    "RGTR_SN AS rgtr_sn, "
    "RGTR_ID AS rgtr_id, "
    "RGTR_NM AS rgtr_nm, "
    "RGTR_IP_ADDR AS rgtr_ip_addr, "
    "RGTR_BRWSR AS rgtr_brwsr, "
    "REG_DT AS reg_dt, "
    "MDFR_SN AS mdfr_sn, "
    "MDFR_ID AS mdfr_id, "
    "MDFR_NM AS mdfr_nm, "
    "MDFR_IP_ADDR AS mdfr_ip_addr, "
    "MDFR_BRWSR AS mdfr_brwsr, "
    "MDFCN_DT AS mdfcn_dt, "
    "USE_YN AS use_yn, "
    "INQ_CNT AS inq_cnt, "
    "APLY_QLFC AS aply_qlfc, "
    "BSS AS bss, "
    "PRPS AS prps, "
    "PVSN_TYPE AS pvsn_type, "
    "SPRT_CN AS sprt_cn, "
    "SPRT_TRGT AS sprt_trgt, "
    "ENFC_MNBD AS enfc_mnbd, "
    "APLY_MTHD AS aply_mthd, "
    "INQPL AS inqpl, "
    "TKCG_DEPT AS tkcg_dept, "
    "TELNO AS telno, "
    "SBMSN_DCMNT AS sbmsn_dcmnt, "
    "SPRT_TRGT_CN AS sprt_trgt_cn, "
    "ENFC_MNBD_CN AS enfc_mnbd_cn, "
    "ITRST_TPC AS itrst_tpc, "
    "ORG_NM AS org_nm, "
    "FILE_DIR AS file_dir, "
    "UUID_NM AS uuid_nm, "
    "REMARK AS remark, "
    "ITRST_TPC1 AS itrst_tpc1, "
    "ITRST_TPC2 AS itrst_tpc2"
)

# 관리자가 수정 가능한 필드 → DB 컬럼명 매핑
_WRITE_COL_MAP: dict[str, str] = {
    "wlf_yr": "WLF_YR",
    "wlf_srvc_nm": "WLF_SRVC_NM",
    "sigun_cd": "SIGUN_CD",
    "use_yn": "USE_YN",
    "aply_yn": "APLY_YN",
    "remark": "REMARK",
    "itrst_tpc1": "ITRST_TPC1",
    "itrst_tpc2": "ITRST_TPC2",
    "telno": "TELNO",
    "tkcg_dept": "TKCG_DEPT",
    "inqpl": "INQPL",
}


def _quote(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError(f"허용되지 않는 식별자: {name!r}")
    return f"`{name}`"


class WlfSrvcRepository:
    def __init__(self, conn: Any, table: str = "tbl_wlf_srvc") -> None:
        self._conn = conn
        self._table = _quote(table)

    def list(
        self,
        *,
        wlf_yr: str | None = None,
        sigun_cd: str | None = None,
        use_yn: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int]:
        where: list[str] = []
        params: list[Any] = []

        if wlf_yr:
            where.append("WLF_YR = %s")
            params.append(wlf_yr)
        if sigun_cd:
            where.append("SIGUN_CD = %s")
            params.append(sigun_cd)
        if use_yn:
            where.append("USE_YN = %s")
            params.append(use_yn)
        if keyword:
            where.append("(WLF_SRVC_NM LIKE %s OR ORG_NM LIKE %s OR TKCG_DEPT LIKE %s)")
            like = f"%{keyword}%"
            params.extend([like, like, like])

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        offset = (page - 1) * page_size

        cursor = self._conn.cursor(dictionary=True)
        try:
            cursor.execute(
                f"SELECT COUNT(*) AS cnt FROM {self._table} {where_sql}", params
            )
            total: int = cursor.fetchone()["cnt"]
            cursor.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} {where_sql} "
                "ORDER BY WLF_SRVC_SN DESC LIMIT %s OFFSET %s",
                [*params, page_size, offset],
            )
            rows = list(cursor.fetchall())
        finally:
            cursor.close()
        return rows, total

    def get(self, record_id: int) -> dict | None:
        cursor = self._conn.cursor(dictionary=True)
        try:
            cursor.execute(
                f"SELECT {_SELECT_COLS} FROM {self._table} WHERE WLF_SRVC_SN = %s",
                (record_id,),
            )
            return cursor.fetchone()
        finally:
            cursor.close()

    def update(self, record_id: int, data: dict) -> bool:
        set_clauses: list[str] = []
        values: list[Any] = []
        for field, col in _WRITE_COL_MAP.items():
            if field in data:
                set_clauses.append(f"`{col}` = %s")
                values.append(data[field])
        if not set_clauses:
            return False
        values.append(record_id)
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                f"UPDATE {self._table} SET {', '.join(set_clauses)} WHERE WLF_SRVC_SN = %s",
                values,
            )
            return cursor.rowcount > 0
        finally:
            cursor.close()

    def delete(self, record_id: int) -> bool:
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                f"DELETE FROM {self._table} WHERE WLF_SRVC_SN = %s", (record_id,)
            )
            return cursor.rowcount > 0
        finally:
            cursor.close()
