"""
OUR_REGION_TEL CSV 직접 검색

GSND_OUR_REGION_TEL Mariner 검색 실패(결과 없음 또는 예외) 시
260422_our_region_tel.csv에서 직접 조회합니다.
컬럼: 시군구, 센터명, 읍면동, 연락처, 주소
"""

import csv
import logging
from app.core.constants import CSV_WELFARE_BASE_SCORE, CSV_WELFARE_TEL_NAME_BONUS, CSV_WELFARE_TEL_TYPE_BONUS

import os
from functools import lru_cache
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "260422_our_region_tel.csv")


@lru_cache(maxsize=1)
def _load_csv() -> List[Dict[str, str]]:
    """CSV 로드 (프로세스 생애주기 동안 1회 캐싱)"""
    rows = []
    try:
        with open(_CSV_PATH, encoding="utf-8-sig", newline="") as f:
            for idx, row in enumerate(csv.DictReader(f)):
                if row.get("센터명", "").strip():
                    rows.append({**row, "_idx": idx})
        logger.info("[CSV/welfare_tel] 로드 완료: %d개", len(rows))
    except Exception as e:
        logger.error("[CSV/welfare_tel] 로드 실패: %s", e)
    return rows


def search_welfare_tel_from_csv(
    keyword: str,
    sigun_filters: Optional[List[str]] = None,
    eupmyeondong_filters: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    OUR_REGION_TEL CSV 직접 검색

    Args:
        keyword: 검색 키워드 (자연어 또는 토큰 나열)
        sigun_filters: ["경상남도 창원시"] 형태 — 단축형으로 변환 후 주소 포함 여부 검사
        eupmyeondong_filters: ["월영동"] 형태 — 읍면동 컬럼 일치 여부 검사

    Returns:
        Mariner 결과와 동일한 doc dict 목록 (WEIGHT 내림차순 정렬)
    """
    rows = _load_csv()
    if not rows:
        return []

    # sigun 단축형 집합 구성 ("경상남도 창원시" → "창원시")
    sigun_shorts: set = set()
    skip_sigun_filter = False
    for s in (sigun_filters or []):
        s = str(s).strip()
        if s == "경상남도":
            skip_sigun_filter = True
            break
        short = s.split(" ", 1)[1] if " " in s else s
        sigun_shorts.add(short)

    # 읍면동 필터 집합
    emd_set = {e.strip() for e in (eupmyeondong_filters or []) if e and e.strip()}

    # 키워드 토큰 (공백/쉼표 구분)
    keyword_tokens = [t for t in keyword.replace(",", " ").split() if len(t) > 1]

    results: List[Dict[str, Any]] = []

    for row in rows:
        center = row.get("센터명", "").strip()
        emd = row.get("읍면동", "").strip()
        sigun = row.get("시군구", "").strip()
        address = row.get("주소", "").strip()
        tel = row.get("연락처", "").strip()

        # ── SIGUN 필터 ──────────────────────────────────────────
        if not skip_sigun_filter and sigun_shorts:
            if not any(short in address or short == sigun for short in sigun_shorts):
                continue

        # ── 읍면동 필터 ─────────────────────────────────────────
        if emd_set:
            if not any(e == emd or e in center for e in emd_set):
                continue

        # ── 키워드 매칭 ─────────────────────────────────────────
        if keyword_tokens:
            matched = any(
                t in center or t in emd or t in address
                for t in keyword_tokens
            )
            if not matched:
                continue

        # ── 점수 계산 ───────────────────────────────────────────
        score = CSV_WELFARE_BASE_SCORE
        for t in keyword_tokens:
            if t in center:
                score += CSV_WELFARE_TEL_NAME_BONUS
            if t == emd:
                score += CSV_WELFARE_TEL_TYPE_BONUS
        score = min(round(score, 4), 1.0)

        # ── doc 구성 (Mariner 결과 포맷과 동일) ─────────────────
        snippet_parts = []
        for field, label in [
            ("센터명",  "센터명"),
            ("읍면동",  "읍면동"),
            ("연락처",  "연락처"),
            ("주소",    "주소"),
        ]:
            val = row.get(field, "").strip()
            if val:
                snippet_parts.append(f"{label}: {val}")

        doc: Dict[str, Any] = {
            "ID":           f"csv_{row['_idx']}",
            "CHUNK_ID":     f"csv_{row['_idx']}",
            "SIGUN":        sigun,
            "CENTER":       center,
            "EUPMYEONDONG": emd,
            "TEL":          tel,
            "ADDRESS":      address,
            "WEIGHT":       str(score),
            "NAME":         center,
            "CHUNK_PATH":   "\n".join(snippet_parts),
            "_source":      "our_region_tel_csv",
        }
        results.append(doc)

    results.sort(key=lambda x: float(x.get("WEIGHT", 0)), reverse=True)
    logger.info(
        "[CSV/welfare_tel] 검색 완료: %d개 (keyword=%r, sigun=%s, emd=%s)",
        len(results), keyword[:40], list(sigun_shorts), list(emd_set),
    )
    return results
