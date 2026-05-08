"""MySQL DB 직접 조회 (복지 전화번호, 복지시설)"""

import logging
from typing import Any, Dict, List, Optional

from app.core.config import Config

logger = logging.getLogger(__name__)


def _lookup_welfare_tel(doc: Dict[str, Any]) -> Optional[List[Dict[str, str]]]:
    """{year}_WELFARE_TEL 테이블에서 읍면동별 실제 전화번호 조회.

    Args:
        doc: OKMS 문서 딕셔너리 (BUSINESS_NAME, ORG_NM, YEAR 필드 사용)

    Returns:
        [{"읍면동": ..., "전화번호": ..., "업무내용": ...}, ...] 또는 None
    """
    import mysql.connector

    raw_year = str(doc.get("YEAR", "") or "").strip()
    year = raw_year[:4] if len(raw_year) >= 4 else raw_year
    if not year.isdigit():
        return None

    table_name = f"{year}_WELFARE_TEL"
    business_name = str(doc.get("BUSINESS_NAME", "") or "").strip()
    org_nm = str(doc.get("ORG_NM", "") or "").strip()

    if not business_name and not org_nm:
        return None

    try:
        conn = mysql.connector.connect(
            host=Config.DB_HOST,
            port=Config.DB_PORT,
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            database=Config.OKMS2_DB_NAME,
            autocommit=True,
        )
        cursor = conn.cursor(dictionary=True)

        if business_name:
            cursor.execute(
                f"SELECT `문의처 및 담당부서`, 연락처, 담당업무 FROM `{table_name}` WHERE 사업명 = %s",
                (business_name,)
            )
            rows = cursor.fetchall()
            if rows:
                logger.info(f"[WelfareTel] 사업명='{business_name}' → {table_name} 조회 성공 ({len(rows)}건)")
                cursor.close()
                conn.close()
                return [dict(r) for r in rows]

        if org_nm:
            cursor.execute(
                f"SELECT `문의처 및 담당부서`, 연락처, 담당업무 FROM `{table_name}` WHERE 문서명 = %s",
                (org_nm,)
            )
            rows = cursor.fetchall()
            if rows:
                logger.info(f"[WelfareTel] 문서명='{org_nm}' → {table_name} 조회 성공 ({len(rows)}건)")
                cursor.close()
                conn.close()
                return [dict(r) for r in rows]

        cursor.close()
        conn.close()
        logger.info(f"[WelfareTel] {table_name} 조회 결과 없음 (사업명='{business_name}', 파일명='{org_nm}')")
        return None
    except Exception as e:
        logger.warning(f"[WelfareTel] DB 조회 실패 (table={table_name}): {e}")
        return None


def _lookup_facility_from_db(facility_name: str) -> List[Dict[str, Any]]:
    """okms2.TB_OFFICE 에서 시설명 LIKE 검색 후 welfare center 스키마로 변환"""
    import mysql.connector
    try:
        conn = mysql.connector.connect(
            host=Config.DB_HOST,
            port=Config.DB_PORT,
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            database=Config.OKMS2_DB_NAME,
            autocommit=True,
        )
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT 시설명, 시군, 정제도로명주소, 소재지, 시설종류, 시설분류, 정원, homepage, tel"
            " FROM TB_OFFICE WHERE 시설명 LIKE %s LIMIT %s",
            (f"%{facility_name}%", Config.RAG_NUM_REFERENCED_DOCS),
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        _EMPTY_HOMEPAGE = {"없음", "없슴", "해당없음", "-", "N/A", "n/a", ""}
        result = []
        for row in rows:
            address  = str(row.get("정제도로명주소") or row.get("소재지") or "").strip()
            fac_type = str(row.get("시설종류") or "").strip()
            fac_cat  = str(row.get("시설분류") or "").strip()
            homepage = str(row.get("homepage") or "").strip()
            tel      = str(row.get("tel") or "").strip()
            clean_hp = homepage if homepage not in _EMPTY_HOMEPAGE else ""
            snippet_parts = []
            for label, val in [("시설유형", fac_type), ("분류", fac_cat),
                                ("주소", address), ("전화", tel), ("홈페이지", clean_hp)]:
                if val:
                    snippet_parts.append(f"{label}: {val}")
            result.append({
                "FACILITY_NAME":     str(row.get("시설명") or "").strip(),
                "SIGUN":             str(row.get("시군") or "").strip(),
                "ADDRESS":           address,
                "FACILITY_TYPE":     fac_type,
                "FACILITY_CATEGORY": fac_cat,
                "FACILITY_CAPACITY": str(row.get("정원") or "").strip(),
                "HOMEPAGE":          clean_hp,
                "TEL":               tel,
                "CHUNK_ID":          "",
                "ID":                "",
                "WEIGHT":            "1.0",
                "NAME":              str(row.get("시설명") or "").strip(),
                "CHUNK_PATH":        "\n".join(snippet_parts),
            })
        logger.info(f"[Welfare/DB] '{facility_name}' DB 조회 결과: {len(result)}개")
        return result
    except Exception as e:
        logger.warning(f"[Welfare/DB] DB 조회 실패: {e}")
        return []
