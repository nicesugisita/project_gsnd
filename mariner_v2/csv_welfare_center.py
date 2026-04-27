"""
WELFARE_CENTER CSV 직접 검색

GSND_WELFARE_CENTER_V1 Mariner 검색 실패(결과 없음 또는 예외) 시
260422_TB_OFFICE.csv에서 직접 조회합니다.

컬럼: id, 시군, 시설명, 경위도X좌표, 경위도Y좌표, 정제도로명주소, 정제지번주소,
       소재지, 시설종류, 시설분류, 정원, 대분류~소분류3, homepage, tel,
       created_at, updated_at
인코딩: cp949
"""

import csv
import logging
from core.constants import CSV_WELFARE_BASE_SCORE, CSV_WELFARE_CENTER_NAME_BONUS, CSV_WELFARE_CENTER_TYPE_BONUS

import os
from functools import lru_cache
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "260422_TB_OFFICE.csv")

# homepage 값이 실질적으로 없는 경우 처리
_EMPTY_HOMEPAGE = {"없음", "없슴", "해당없음", "-", "N/A", "n/a", ""}


@lru_cache(maxsize=1)
def _load_csv() -> List[Dict[str, str]]:
    """CSV 로드 (프로세스 생애주기 동안 1회 캐싱)"""
    rows = []
    try:
        with open(_CSV_PATH, encoding="cp949", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("시설명", "").strip():
                    rows.append(row)
        logger.info("[CSV/welfare_center] 로드 완료: %d개", len(rows))
    except Exception as e:
        logger.error("[CSV/welfare_center] 로드 실패: %s", e)
    return rows


def search_welfare_center_from_csv(
    keyword: str,
    sigun_filters: Optional[List[str]] = None,
    facility_type_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    WELFARE_CENTER CSV 직접 검색

    Args:
        keyword: 검색 키워드 (자연어 또는 토큰 나열)
        sigun_filters: ["경상남도 창원시"] 형태 — CSV 시군 컬럼(전체형)과 직접 비교
        facility_type_filter: "노인복지관" 등 — 시설종류 컬럼과 비교

    Returns:
        query_welfare_center_documents()와 동일한 doc dict 목록 (WEIGHT 내림차순)
    """
    rows = _load_csv()
    if not rows:
        return []

    # sigun 필터: CSV 시군 컬럼은 "경상남도 창원시" 전체형 → 그대로 비교
    skip_sigun_filter = False
    sigun_set: set = set()
    sigun_shorts: set = set()
    for s in (sigun_filters or []):
        s = str(s).strip()
        if s == "경상남도":
            skip_sigun_filter = True
            break
        sigun_set.add(s)
        short = s.split(" ", 1)[1] if " " in s else s
        sigun_shorts.add(short)

    # 키워드 토큰 (길이 2 이상만)
    keyword_tokens = [t for t in keyword.replace(",", " ").split() if len(t) >= 2]

    results: List[Dict[str, Any]] = []

    for row in rows:
        name      = row.get("시설명", "").strip()
        sigun     = row.get("시군", "").strip()
        address   = row.get("정제도로명주소", "").strip() or row.get("소재지", "").strip()
        fac_type  = row.get("시설종류", "").strip()
        fac_cat   = row.get("시설분류", "").strip()
        capacity  = row.get("정원", "").strip()
        homepage  = row.get("homepage", "").strip()
        tel       = row.get("tel", "").strip()

        # ── SIGUN 필터 ──────────────────────────────────────────
        if not skip_sigun_filter and sigun_set:
            # 전체형 직접 비교 우선, 단축형 포함 여부 보조
            if sigun not in sigun_set and not any(short in sigun for short in sigun_shorts):
                continue

        # ── 시설종류 필터 ────────────────────────────────────────
        if facility_type_filter and fac_type != facility_type_filter:
            continue

        # ── 키워드 매칭 ─────────────────────────────────────────
        if keyword_tokens:
            searchable = f"{name} {address} {fac_type}"
            if not any(t in searchable for t in keyword_tokens):
                continue

        # ── 점수 계산 ───────────────────────────────────────────
        score = CSV_WELFARE_BASE_SCORE
        for t in keyword_tokens:
            if t in name:
                score += CSV_WELFARE_CENTER_NAME_BONUS
            elif t in fac_type:
                score += CSV_WELFARE_CENTER_TYPE_BONUS
        score = min(round(score, 4), 1.0)

        # ── homepage 정제 ────────────────────────────────────────
        clean_homepage = homepage if homepage not in _EMPTY_HOMEPAGE else ""

        # ── CHUNK_PATH 스니펫 (query_welfare_center_documents 포맷과 동일) ──
        snippet_parts = []
        for label, val in [
            ("시설유형", fac_type),
            ("분류",     fac_cat),
            ("주소",     address),
            ("전화",     tel),
            ("홈페이지", clean_homepage),
        ]:
            if val:
                snippet_parts.append(f"{label}: {val}")

        doc: Dict[str, Any] = {
            # Mariner 결과 포맷과 동일한 키 사용
            "ID":                row.get("id", ""),
            "CHUNK_ID":          f"csv_{row.get('id', '')}",
            "SIGUN":             sigun,
            "FACILITY_NAME":     name,
            "ADDRESS":           address,
            "FACILITY_TYPE":     fac_type,
            "FACILITY_CATEGORY": fac_cat,
            "FACILITY_CAPACITY": capacity,
            "HOMEPAGE":          clean_homepage,
            "TEL":               tel,
            "WEIGHT":            str(score),
            # rag_service 공통 필드
            "NAME":              name,
            "CHUNK_PATH":        "\n".join(snippet_parts),
            "_source":           "welfare_center_csv",
        }
        results.append(doc)

    results.sort(key=lambda x: float(x.get("WEIGHT", 0)), reverse=True)
    logger.info(
        "[CSV/welfare_center] 검색 완료: %d개 (keyword=%r, sigun=%s, fac_type=%s)",
        len(results), keyword[:40], list(sigun_set), facility_type_filter,
    )
    return results
