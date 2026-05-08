"""메시지 파싱 유틸리티 (시군, 연도, 생애주기, 문서 정규화)"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.mariner.sigun_utils import _SIGUN_NORMALIZE_MAP

_LIFECYCLE_CONTENT_KEYWORDS: Dict[str, List[str]] = {
    "영유아": ["영유아"],
    "아동": ["아동"],
    "청소년": ["청소년"],
    "청년": ["청년","대학생"],
    "중장년": ["중장년"],
    "노인": ["노인", "노년"],
    "임신·출산": ["임신", "임산부", "산모", "예비 엄마", "예비엄마", "예비 부모", "예비부모", "출산 준비", "출산준비"],
}

_LIFECYCLE_KEYWORD_MAP: Dict[str, str] = {
    "영유아": "영유아",
    "신생아": "영유아",
    "영아": "영유아",
    "유아": "영유아",
    "유치원": "영유아",
    "아동": "아동",
    "초등학생": "아동",
    "초등생": "아동",
    "청소년": "청소년",
    "중학생": "청소년",
    "고등학생": "청소년",
    "청년": "청년",
    "대학생": "청년",
    "중장년": "중장년",
    "장년": "중장년",
    "노인": "노인",
    "노년": "노인",
    "시니어": "노인",
    "어르신": "노인",
    "고령": "노인",
        # 난임 단계
    "난임": "임신·출산",
    "난임치료": "임신·출산",
    "시험관": "임신·출산",
    "시험관아기": "임신·출산",
    "인공수정": "임신·출산",

    # 임신 단계
    "임신": "임신·출산",
    "임산부": "임신·출산",
    "고위험임산부": "임신·출산",
    "태아": "임신·출산",
    "산전": "임신·출산",
    "출산예정": "임신·출산",

    # 예비 부모
    "예비엄마": "임신·출산",
    "예비 엄마": "임신·출산",
    "예비부모": "임신·출산",
    "예비 부모": "임신·출산",

    # 출산 단계
    "출산": "임신·출산",
    "출산준비": "임신·출산",
    "출산 준비": "임신·출산",
    "분만": "임신·출산",
    "산모": "임신·출산",

    # 산후 단계
    "산후": "임신·출산",
    "산후조리": "임신·출산",
    "산후조리원": "임신·출산",

    # 신생아 초기
    "신생아": "임신·출산",
    "영아": "임신·출산",

    # 육아 초기 (출산 직후 정책 포함)
    "육아": "임신·출산",
    "육아휴직": "임신·출산",
    "배우자출산휴가": "임신·출산",

    # 정책/지원 표현
    "출산지원": "임신·출산",
    "출산지원금": "임신·출산",
    "출산장려금": "임신·출산",
    "출산혜택": "임신·출산",
    "출산비": "임신·출산",
}


def _extract_sigun_from_message(message: str) -> List[str]:
    """사용자 메시지에서 경상남도 시·군 이름을 모두 추출하여 반환합니다."""
    if not message:
        return []
    found: List[str] = []
    seen: set = set()
    for key, full_name in _SIGUN_NORMALIZE_MAP.items():
        if full_name in message and key not in seen:
            found.append(key)
            seen.add(key)
    for key in _SIGUN_NORMALIZE_MAP:
        if key not in seen and re.search(rf'{re.escape(key)}[시군]?', message):
            found.append(key)
            seen.add(key)
    if found:
        return found
    if re.search(r'경상남도|경남', message):
        return ["경남"]
    return []


def _extract_years_from_message(message: str) -> List[str]:
    """사용자 메시지에서 4자리 연도 목록을 추출합니다. 연도 미감지 시 현재 연도 반환."""
    from app.shared.utils.year_filter import extract_year_filters
    return extract_year_filters(message)


def _extract_birth_year_from_message(message: str) -> Optional[int]:
    """사용자 메시지에서 출생연도(4자리)를 추출합니다."""
    if not message:
        return None
    # 패턴1: "2020년생" / "2020생" / "2020년 출생" — 명시적 출생 표현
    match = re.search(r'(19[0-9]{2}|20[01][0-9]|202[0-6])\s*년?\s*(?:생|출생)', message)
    if match:
        return int(match.group(1))
    # 패턴2: 나이 표현 (예: "35세", "5살")
    age_match = re.search(r'(?:나이\s*)?(\d{1,2})\s*(?:세|살)', message)
    if age_match:
        return datetime.now().year - int(age_match.group(1))
    # 패턴3: "YYYY년"이 문장 끝에 위치 (예: "창원 2020년") — reformed query의 "2026년 창원시..." 오인식 방지
    end_year_match = re.search(r'(19[0-9]{2}|20[01][0-9]|202[0-6])\s*년\s*$', message.strip())
    if end_year_match:
        return int(end_year_match.group(1))
    # 패턴4: 단독 4자리 연도 (예: "거제 2020") — "년" 뒤에 오는 경우 제외
    bare_match = re.search(r'(?<!\d)(19[0-9]{2}|20[01][0-9]|202[0-6])(?!\d)(?!\s*년)', message)
    if bare_match:
        return int(bare_match.group(1))
    return None


def _extract_lifecycle_from_message(message: str) -> str:
    """메시지에서 생애주기 키워드를 직접 추출합니다."""
    for keyword, lifecycle in _LIFECYCLE_KEYWORD_MAP.items():
        if keyword in message:
            return lifecycle
    # "60대", "70대", "80대" 등 연령대 표현 처리
    decade_match = re.search(r'(\d+)\s*대', message)
    if decade_match:
        decade = int(decade_match.group(1))
        if decade >= 60:
            return "노인"
        elif decade >= 40:
            return "중장년"
        elif decade >= 20:
            return "청년"
        elif decade >= 10:
            return "청소년"
    return ""


def _birth_year_to_lifecycle(birth_year: int, current_year: int = None) -> str:
    """출생연도를 생애주기 라벨로 변환합니다."""
    if current_year is None:
        current_year = datetime.now().year
    age = current_year - birth_year
    if age <= 5:
        return "영유아"
    elif age <= 12:
        return "아동"
    elif age <= 18:
        return "청소년"
    elif age <= 39:
        return "청년"
    elif age <= 64:
        return "중장년"
    else:
        return "노인"


def _filter_docs_by_lifecycle(docs: List[Dict[str, Any]], lifecycle: str) -> List[Dict[str, Any]]:
    """생애주기 키워드가 CONTENT에 포함된 문서만 반환합니다."""
    keywords = _LIFECYCLE_CONTENT_KEYWORDS.get(lifecycle, [])
    if not keywords:
        return docs
    return [
        doc for doc in docs
        if str(doc.get("LIFE_CYCLE", "") or "").strip() in keywords
    ]


def _normalize_sigun_docs(raw_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """mariner_queryset.mariner_search() 반환 형식을 query_mariner_documents() 형식으로 변환합니다."""
    from .document import _build_okms_document_name
    normalized = []
    for doc in raw_docs:
        n = {
            "CHUNK_ID": str(doc.get("chunk_id", "") or ""),
            "YEAR": str(doc.get("year", "") or ""),
            "SIGUN": str(doc.get("sigun", "") or ""),
            "CONTENT": str(doc.get("content", "") or ""),
            "WEIGHT": str(doc.get("score", "0") or "0"),
            "LIFE_CYCLE": str(doc.get("life_cycle", "") or ""),
            "BUSINESS_NAME": str(doc.get("business_name", "") or ""),
            "DEPARTMENT": str(doc.get("department", "") or ""),
            "ORG_NM": str(doc.get("org_nm", "") or ""),
            "PATH": str(doc.get("path", "") or ""),
        }
        n["NAME"] = _build_okms_document_name(n)
        n["CHUNK_PATH"] = n["CONTENT"]
        normalized.append(n)
    return normalized
