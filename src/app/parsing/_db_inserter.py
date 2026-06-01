"""parse_hwpx_v6_db.py의 DB 적재 로직을 mysql.connector 방식으로 재구현한 모듈.

parse_hwpx_v6_db.py의 전역 설정값 대신 함수 인자로 동작하므로,
FastAPI 의존성 주입 패턴과 자연스럽게 연결된다.
"""

from __future__ import annotations

from datetime import datetime
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# INSERT 컬럼 순서 (WLF_SRVC_SN 제외, DB AUTO_INCREMENT가 생성)
INSERT_COLUMNS = [
    "WLF_SRVC_APLY_FORM_SN",
    "WLF_YR",
    "WLF_SRVC_NM",
    "LFTM_CYCL_CD",
    "HSHD_STTN_CD",
    "SIGUN_CD",
    "WLF_SRVC_CN",
    "APLY_YN",
    "APLY_BGNG_DT",
    "APLY_END_DT",
    "APLY_PRD_TYPE",
    "RGTR_SN",
    "RGTR_ID",
    "RGTR_NM",
    "RGTR_IP_ADDR",
    "RGTR_BRWSR",
    "REG_DT",
    "MDFR_SN",
    "MDFR_ID",
    "MDFR_NM",
    "MDFR_IP_ADDR",
    "MDFR_BRWSR",
    "MDFCN_DT",
    "USE_YN",
    "INQ_CNT",
    "APLY_QLFC",
    "BSS",
    "PRPS",
    "PVSN_TYPE",
    "SPRT_CN",
    "SPRT_TRGT",
    "ENFC_MNBD",
    "APLY_MTHD",
    "INQPL",
    "TKCG_DEPT",
    "TELNO",
    "SBMSN_DCMNT",
    "SPRT_TRGT_CN",
    "ENFC_MNBD_CN",
    "ORG_NM",
    "FILE_DIR",
    "UUID_NM",
    "REMARK",
    "ITRST_TPC1",
    "ITRST_TPC2",
]

# tb_cd를 읽지 못할 때 사용하는 fallback 시군코드 맵
_SIGUN_CD_FALLBACK: dict[str, str] = {
    "경상남도": "4800000000",
    "복지부": "0001000000",
    "국민건강보험공단": "0050000000",
    "창원시": "4812000000",
    "진주시": "4817000000",
    "통영시": "4822000000",
    "사천시": "4824000000",
    "김해시": "4825000000",
    "밀양시": "4827000000",
    "거제시": "4831000000",
    "양산시": "4833000000",
    "의령군": "4872000000",
    "함안군": "4873000000",
    "창녕군": "4874000000",
    "고성군": "4882000000",
    "남해군": "4884000000",
    "하동군": "4885000000",
    "산청군": "4886000000",
    "함양군": "4887000000",
    "거창군": "4888000000",
    "합천군": "4889000000",
}

_TB_CD_SIGUN_CTGRY = "4800000000"
_TB_CD_ROOT_CTGRY = "0000000000"

# 시스템 필드 기본값 (등록자/수정자 정보)
_DEFAULT_RGTR_ID = "F6EDF32073E0B6C7D5C4ACD8C7749C39"
_DEFAULT_RGTR_NM = "25EF4517089D7E4FB3D19B74BB029AF2"
_DEFAULT_MDFR_ID = "F6EDF32073E0B6C7D5C4ACD8C7749C39"
_DEFAULT_MDFR_NM = "F6EDF32073E0B6C7D5C4ACD8C7749C39"


def _quote_ident(name: str) -> str:
    """SQL 식별자(테이블명/컬럼명)를 안전하게 감싼다. 값 바인딩은 별도로 처리."""
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError(f"허용되지 않는 식별자: {name!r}")
    return f"`{name}`"


def to_db_val(value: Any) -> str | None:
    """파서가 만든 값(list/빈 문자열/None)을 DB 저장용 문자열 또는 None으로 정리."""
    if value is None:
        return None
    if isinstance(value, list):
        value = "\n".join(str(v) for v in value)
    text = str(value).strip()
    return text if text else None


def _table_exists(conn: Any, table_name: str) -> bool:
    """테이블 존재 여부를 확인한다."""
    cursor = conn.cursor()
    try:
        cursor.execute("SHOW TABLES LIKE %s", (table_name,))
        return cursor.fetchone() is not None
    finally:
        cursor.close()


def load_sigun_cd_map(conn: Any) -> dict[str, str]:
    """tb_cd를 기준으로 시군코드 맵을 구성한다. 실패 시 fallback 맵 반환."""
    cd_map = dict(_SIGUN_CD_FALLBACK)

    if not _table_exists(conn, "tb_cd"):
        logger.warning("시군코드 참조 테이블 없음: tb_cd / fallback 맵 사용")
        return cd_map

    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT CTGRY, CD, CD_NM FROM tb_cd "
            "WHERE CTGRY IN (%s, %s) AND CD IS NOT NULL AND CD_NM IS NOT NULL",
            (_TB_CD_SIGUN_CTGRY, _TB_CD_ROOT_CTGRY),
        )
        for row in cursor.fetchall():
            cd_nm = to_db_val(row["CD_NM"])
            cd = to_db_val(row["CD"])
            if not cd_nm or not cd:
                continue
            cd_map[cd_nm] = cd
            if row["CTGRY"] == _TB_CD_SIGUN_CTGRY and cd_nm.startswith("경상남도 "):
                short_nm = cd_nm.removeprefix("경상남도 ").strip()
                if short_nm and " " not in short_nm:
                    cd_map[short_nm] = cd
    finally:
        cursor.close()

    logger.debug("시군코드 매핑: tb_cd 기준 %d개 / 창원시=%s", len(cd_map), cd_map.get("창원시"))
    return cd_map


def get_sigun_cd(row: dict, sigun_cd_map: dict[str, str]) -> str:
    """row의 SIGUN_NM 또는 ORG_NM에서 시군코드를 추론한다."""
    org_nm = row.get("ORG_NM") or ""
    if org_nm.startswith("국민건강보험공단_"):
        return sigun_cd_map.get("국민건강보험공단", sigun_cd_map["경상남도"])
    if org_nm.startswith("2026_도_"):
        return sigun_cd_map["경상남도"]
    if org_nm.startswith("2026_복지부"):
        return sigun_cd_map.get("복지부", sigun_cd_map["경상남도"])

    sigun_nm = row.get("SIGUN_NM") or ""
    if sigun_nm in sigun_cd_map:
        return sigun_cd_map[sigun_nm]
    for nm, cd in sigun_cd_map.items():
        if nm != "경상남도" and nm in sigun_nm:
            return cd
    for nm, cd in sigun_cd_map.items():
        if nm not in ("경상남도", "복지부", "국민건강보험공단") and nm in org_nm:
            return cd
    return sigun_cd_map["경상남도"]


def _get_table_uuid_map(conn: Any, table_name: str) -> dict[str, str]:
    """테이블에서 ORG_NM → UUID_NM 매핑을 가져온다."""
    if not _table_exists(conn, table_name):
        return {}
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"SELECT ORG_NM, MIN(UUID_NM) AS UUID_NM FROM {_quote_ident(table_name)} "
            "WHERE ORG_NM IS NOT NULL AND UUID_NM IS NOT NULL GROUP BY ORG_NM"
        )
        return {r["ORG_NM"]: r["UUID_NM"] for r in cursor.fetchall()}
    finally:
        cursor.close()


def get_existing_uuid_map(
    conn: Any,
    target_table: str,
    source_uuid_table: str,
) -> dict[str, str]:
    """기준/대상 테이블의 UUID_NM을 병합해 반환한다. 기준 테이블 값이 우선."""
    target_map = _get_table_uuid_map(conn, target_table)
    source_map = _get_table_uuid_map(conn, source_uuid_table)
    merged = {**target_map, **source_map}
    logger.debug(
        "UUID 매핑: 기준 %d건 / 대상 %d건 / 적용 %d건",
        len(source_map), len(target_map), len(merged),
    )
    return merged


def row_to_params(row: dict, sigun_cd_map: dict[str, str]) -> list[Any]:
    """DB row dict를 INSERT_COLUMNS 순서의 파라미터 리스트로 변환한다."""
    now = datetime.now()
    sprt_cn = to_db_val(row.get("SPRT_CN"))
    return [
        13,                                          # WLF_SRVC_APLY_FORM_SN
        to_db_val(row.get("WLF_YR")) or "2025",     # WLF_YR
        to_db_val(row.get("WLF_SRVC_NM")),          # WLF_SRVC_NM
        to_db_val(row.get("LFTM_CYCL_CD")) or "",   # LFTM_CYCL_CD
        to_db_val(row.get("HSHD_STTN_CD")) or "",   # HSHD_STTN_CD
        get_sigun_cd(row, sigun_cd_map),             # SIGUN_CD
        sprt_cn,                                     # WLF_SRVC_CN
        "Y",                                         # APLY_YN
        to_db_val(row.get("APLY_BGNG_DT")),         # APLY_BGNG_DT
        to_db_val(row.get("APLY_END_DT")),          # APLY_END_DT
        to_db_val(row.get("APLY_PRD_TYPE")),        # APLY_PRD_TYPE
        0,                                           # RGTR_SN
        _DEFAULT_RGTR_ID,                            # RGTR_ID
        _DEFAULT_RGTR_NM,                            # RGTR_NM
        "0:0:0:0:0:0:0:1",                          # RGTR_IP_ADDR
        "Mozilla/5.0",                               # RGTR_BRWSR
        now,                                         # REG_DT
        0,                                           # MDFR_SN
        _DEFAULT_MDFR_ID,                            # MDFR_ID
        _DEFAULT_MDFR_NM,                            # MDFR_NM
        "0:0:0:0:0:0:0:1",                          # MDFR_IP_ADDR
        "Mozilla/5.0",                               # MDFR_BRWSR
        now,                                         # MDFCN_DT
        "Y",                                         # USE_YN
        0,                                           # INQ_CNT
        to_db_val(row.get("APLY_QLFC")),            # APLY_QLFC
        to_db_val(row.get("BSS")),                   # BSS
        to_db_val(row.get("PRPS")),                  # PRPS
        to_db_val(row.get("PVSN_TYPE")),             # PVSN_TYPE
        sprt_cn,                                     # SPRT_CN
        to_db_val(row.get("SPRT_TRGT")),            # SPRT_TRGT
        to_db_val(row.get("ENFC_MNBD")),            # ENFC_MNBD
        to_db_val(row.get("APLY_MTHD")),            # APLY_MTHD
        to_db_val(row.get("INQPL")),                 # INQPL
        to_db_val(row.get("TKCG_DEPT")),            # TKCG_DEPT
        to_db_val(row.get("TELNO")),                 # TELNO
        to_db_val(row.get("SBMSN_DCMNT")),          # SBMSN_DCMNT
        to_db_val(row.get("SPRT_TRGT_CN")),         # SPRT_TRGT_CN
        to_db_val(row.get("ENFC_MNBD_CN")),         # ENFC_MNBD_CN
        to_db_val(row.get("ORG_NM")),               # ORG_NM
        to_db_val(row.get("FILE_DIR")),              # FILE_DIR
        to_db_val(row.get("UUID_NM")),              # UUID_NM
        to_db_val(row.get("REMARK")),               # REMARK
        to_db_val(row.get("ITRST_TPC1")),           # ITRST_TPC1
        to_db_val(row.get("ITRST_TPC2")),           # ITRST_TPC2
    ]


def insert_rows(
    conn: Any,
    target_table: str,
    rows: list[dict],
    *,
    delete_existing: bool = True,
    source_uuid_table: str = "tbl_wlf_srvc",
) -> tuple[int, int]:
    """rows를 target_table에 적재하고 (deleted_count, inserted_count) 반환.

    1. UUID_NM 재사용: source_uuid_table과 target_table에서 기존 UUID_NM을 조회해 덮어씀.
    2. delete_existing=True이면 같은 ORG_NM의 기존 행을 먼저 삭제한다.
    3. 트랜잭션은 호출자(service)가 관리한다.
    """
    if not rows:
        return 0, 0

    table = _quote_ident(target_table)
    col_sql = ", ".join(_quote_ident(c) for c in INSERT_COLUMNS)
    placeholders = ", ".join(["%s"] * len(INSERT_COLUMNS))
    insert_sql = f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})"

    sigun_cd_map = load_sigun_cd_map(conn)
    uuid_map = get_existing_uuid_map(conn, target_table, source_uuid_table)

    for row in rows:
        org_nm = row.get("ORG_NM")
        if org_nm and uuid_map.get(org_nm):
            row["UUID_NM"] = uuid_map[org_nm]

    deleted_count = 0
    if delete_existing:
        org_names = sorted({row.get("ORG_NM") for row in rows if row.get("ORG_NM")})
        if org_names:
            cursor = conn.cursor()
            try:
                for i in range(0, len(org_names), 500):
                    chunk = org_names[i : i + 500]
                    in_sql = ", ".join(["%s"] * len(chunk))
                    cursor.execute(f"DELETE FROM {table} WHERE ORG_NM IN ({in_sql})", chunk)
                    deleted_count += cursor.rowcount
            finally:
                cursor.close()
            logger.info("기존 행 삭제: %d건 (ORG_NM %d개)", deleted_count, len(org_names))

    inserted_count = 0
    cursor = conn.cursor()
    try:
        for idx, row in enumerate(rows, start=1):
            try:
                cursor.execute(insert_sql, row_to_params(row, sigun_cd_map))
                inserted_count += 1
            except Exception:
                org_nm = row.get("ORG_NM", "")
                svc_nm = row.get("WLF_SRVC_NM", "")
                logger.exception(
                    "INSERT 오류 [%d/%d] ORG_NM=%r WLF_SRVC_NM=%r",
                    idx, len(rows), org_nm, svc_nm,
                )
                raise
    finally:
        cursor.close()

    logger.info("INSERT 완료: %d건 → %s", inserted_count, target_table)
    return deleted_count, inserted_count


_TEMP_WRITE_COL_MAP: dict[str, str] = {
    "wlf_yr": "WLF_YR",
    "wlf_srvc_nm": "WLF_SRVC_NM",
    "sigun_cd": "SIGUN_CD",
    "org_nm": "ORG_NM",
    "lftm_cycl_cd": "LFTM_CYCL_CD",
    "hshd_sttn_cd": "HSHD_STTN_CD",
    "wlf_srvc_cn": "WLF_SRVC_CN",
    "aply_yn": "APLY_YN",
    "aply_bgng_dt": "APLY_BGNG_DT",
    "aply_end_dt": "APLY_END_DT",
    "aply_prd_type": "APLY_PRD_TYPE",
    "use_yn": "USE_YN",
    "aply_qlfc": "APLY_QLFC",
    "bss": "BSS",
    "prps": "PRPS",
    "pvsn_type": "PVSN_TYPE",
    "sprt_cn": "SPRT_CN",
    "sprt_trgt": "SPRT_TRGT",
    "enfc_mnbd": "ENFC_MNBD",
    "aply_mthd": "APLY_MTHD",
    "inqpl": "INQPL",
    "tkcg_dept": "TKCG_DEPT",
    "telno": "TELNO",
    "sbmsn_dcmnt": "SBMSN_DCMNT",
    "sprt_trgt_cn": "SPRT_TRGT_CN",
    "enfc_mnbd_cn": "ENFC_MNBD_CN",
    "remark": "REMARK",
    "itrst_tpc1": "ITRST_TPC1",
    "itrst_tpc2": "ITRST_TPC2",
    "file_dir": "FILE_DIR",
    "uuid_nm": "UUID_NM",
}


def delete_temp_row_by_sn(conn: Any, *, table: str, sn: int) -> bool:
    """temp 테이블에서 단일 행을 삭제한다. 삭제 성공 시 True 반환."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"DELETE FROM {_quote_ident(table)} WHERE WLF_SRVC_SN = %s",
            (sn,),
        )
        return cursor.rowcount > 0
    finally:
        cursor.close()


def delete_temp_rows_by_filter(conn: Any, *, table: str, wlf_yr: str | None = None) -> int:
    """temp 테이블에서 조건에 맞는 행을 일괄 삭제한다. 삭제 건수 반환.

    wlf_yr 지정 시 해당 연도만, None이면 전체 삭제.
    """
    tbl = _quote_ident(table)
    cursor = conn.cursor()
    try:
        if wlf_yr:
            cursor.execute(f"DELETE FROM {tbl} WHERE WLF_YR = %s", (wlf_yr,))
        else:
            cursor.execute(f"DELETE FROM {tbl}")
        count = cursor.rowcount
        logger.info("temp 행 삭제: %d건 (table=%s, wlf_yr=%s)", count, table, wlf_yr)
        return count
    finally:
        cursor.close()


def update_temp_row_in_db(conn: Any, *, table: str, sn: int, data: dict) -> bool:
    """temp 테이블의 특정 행을 업데이트한다. 변경된 필드만 SET 절에 포함."""
    set_clauses: list[str] = []
    values: list[Any] = []
    for field, col in _TEMP_WRITE_COL_MAP.items():
        if field in data:
            set_clauses.append(f"`{col}` = %s")
            values.append(data[field])
    if not set_clauses:
        return False
    values.append(sn)
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"UPDATE {_quote_ident(table)} SET {', '.join(set_clauses)} WHERE WLF_SRVC_SN = %s",
            values,
        )
        return cursor.rowcount > 0
    finally:
        cursor.close()


def publish_rows(
    conn: Any,
    *,
    source_table: str,
    target_table: str,
    year: str | None = None,
    org_names: list[str] | None = None,
) -> tuple[int, int, list[str]]:
    """source_table(temp)의 데이터를 target_table(실 테이블)로 반영한다.

    1. source_table에서 반영 대상 ORG_NM 목록을 조회한다.
    2. target_table에서 해당 ORG_NM 행을 삭제한다.
    3. INSERT SELECT로 source_table → target_table에 복사한다.
    4. 트랜잭션은 호출자(service)가 관리한다.

    Returns:
        (deleted_count, inserted_count, published_org_names)
    """
    src = _quote_ident(source_table)
    tgt = _quote_ident(target_table)

    # 반영 대상 ORG_NM 조회
    cursor = conn.cursor()
    try:
        where_clauses: list[str] = ["ORG_NM IS NOT NULL"]
        params: list[Any] = []

        if year:
            where_clauses.append("WLF_YR = %s")
            params.append(year)

        if org_names:
            placeholders = ", ".join(["%s"] * len(org_names))
            where_clauses.append(f"ORG_NM IN ({placeholders})")
            params.extend(org_names)

        where_sql = " AND ".join(where_clauses)
        cursor.execute(
            f"SELECT DISTINCT ORG_NM FROM {src} WHERE {where_sql} ORDER BY ORG_NM",
            params,
        )
        target_org_names: list[str] = [row[0] for row in cursor.fetchall()]
    finally:
        cursor.close()

    if not target_org_names:
        logger.info("publish: 반영 대상 없음 (source=%s, year=%s)", source_table, year)
        return 0, 0, []

    # target_table에서 기존 행 삭제
    deleted_count = 0
    cursor = conn.cursor()
    try:
        for i in range(0, len(target_org_names), 500):
            chunk = target_org_names[i : i + 500]
            in_sql = ", ".join(["%s"] * len(chunk))
            cursor.execute(f"DELETE FROM {tgt} WHERE ORG_NM IN ({in_sql})", chunk)
            deleted_count += cursor.rowcount
    finally:
        cursor.close()
    logger.info("publish: 기존 행 삭제 %d건 (ORG_NM %d개)", deleted_count, len(target_org_names))

    # INSERT SELECT: INSERT_COLUMNS 기준 (WLF_SRVC_SN 제외)
    col_sql = ", ".join(_quote_ident(c) for c in INSERT_COLUMNS)
    inserted_count = 0
    cursor = conn.cursor()
    try:
        for i in range(0, len(target_org_names), 500):
            chunk = target_org_names[i : i + 500]
            in_sql = ", ".join(["%s"] * len(chunk))
            cursor.execute(
                f"INSERT INTO {tgt} ({col_sql}) "
                f"SELECT {col_sql} FROM {src} WHERE ORG_NM IN ({in_sql})",
                chunk,
            )
            inserted_count += cursor.rowcount
    finally:
        cursor.close()

    logger.info("publish: INSERT 완료 %d건 → %s", inserted_count, target_table)
    return deleted_count, inserted_count, target_org_names
