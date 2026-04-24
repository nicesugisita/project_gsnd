"""시설 키워드 매핑 및 추출"""

import csv
import logging
import os
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_FACILITY_NAME_PATTERN = re.compile(
    r'[가-힣]{2,}\s*'
    r'(?:양로원|양로시설|요양원|요양시설|노인복지관|복지관|경로당|노인교실|'
    r'주간보호센터|주야간보호센터|주야간보호|데이케어센터|'
    r'재가센터|재가서비스|방문요양|방문목욕|노인요양|'
    r'사회복지관|재활원|재활시설|장애인복지관|장애인단체|'
    r'보호작업장|근로작업장|작업장|주간이용시설|'
    r'공동생활가정|그룹홈|거주시설|단기거주시설|생활시설|보호시설|'
    r'아동센터|지역아동센터|어린이집|유치원|아동보호전문기관|돌봄센터|'
    r'청소년센터|청소년쉼터|여성쉼터|쉼터|'
    r'여성회관|가족센터|가족지원센터|'
    r'복지센터|노인센터|케어센터|생활관|노인생활관|생활지원센터|'
    r'자활센터|자활기업|자원봉사센터|'
    r'상담소|수어통역센터|푸드뱅크|사회복지협의회|'
    r'자립생활센터|자립지원센터|장기요양기관|요양병원|정신요양시설|'
    r'의\s*집)'
)


def _load_facility_keyword_map() -> Dict[str, str]:
    """facility.csv에서 용어→시설분류 매핑 테이블 로드"""
    csv_path = os.path.join(os.path.dirname(__file__), '..', '..', 'facility.csv')
    result: Dict[str, str] = {}
    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                facility_type = (row.get('시설분류') or '').strip()
                if not facility_type:
                    continue
                friendly = (row.get('도민 이해용 수정한 시설분류') or '').strip()
                if friendly:
                    result.setdefault(friendly, facility_type)
                for col in ['용어 1', '용어 2', '용어 3', '용어 4', '용어 5', '용어 6']:
                    term = (row.get(col) or '').strip()
                    if term:
                        result.setdefault(term, facility_type)
    except Exception as e:
        logger.warning(f"[Facility] facility.csv 로드 실패: {e}")
    return result


_FACILITY_KEYWORD_MAP: Dict[str, str] = {}


def _get_facility_keyword_map() -> Dict[str, str]:
    """facility.csv 기반 키워드→시설분류 매핑 (지연 로딩)"""
    global _FACILITY_KEYWORD_MAP
    if not _FACILITY_KEYWORD_MAP:
        _FACILITY_KEYWORD_MAP = _load_facility_keyword_map()
        logger.info(f"[Facility] 키워드 매핑 로드 완료: {len(_FACILITY_KEYWORD_MAP)}개")
    return _FACILITY_KEYWORD_MAP


def _extract_facility_type_from_message(message: str) -> Optional[str]:
    """메시지에서 시설 키워드를 추출하여 FACILITY_TYPE 반환. 없으면 None."""
    if not message:
        return None
    keyword_map = _get_facility_keyword_map()
    for keyword, facility_type in keyword_map.items():
        if keyword in message:
            logger.debug(f"[Facility] 키워드 매칭: '{keyword}' → '{facility_type}'")
            return facility_type
    return None


def _extract_specific_facility_name(message: str) -> Optional[str]:
    """메시지에서 구체적 시설명 추출. 없으면 None."""
    if not message:
        return None
    match = _FACILITY_NAME_PATTERN.search(message)
    if not match:
        return None
    return match.group(0).replace(" ", "")
