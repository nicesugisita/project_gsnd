"""메시지 파싱 유틸리티 (시군, 연도, 생애주기, 문서 정규화)"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.mariner.sigun_utils import _SIGUN_NORMALIZE_MAP

# HSHD_STTN_NM(가구상황) 키워드 → DB 정규화값 매핑.
# DB 셀은 CSV 멀티값(예: "저소득,한부모·조손,장애인") 이며, 단일값으로는
# "일반가구"/"저소득"/"장애인"/"한부모·조손"/"다문화·탈북민"/"다자녀" 등이 관측됨.
# WhereSet op=34 (OP_HASANY|QUASI_SYNONYM)로 매칭하므로 정규화값은 셀 토큰과
# 동일한 문자열을 사용해야 한다.
_HSHD_STTN_KEYWORD_MAP: Dict[str, str] = {
    # 저소득 (구체적 신호인 수급자/차상위 계열을 우선 — 길이 동률 시 dict 선언 순서가 tie-breaker)
    "기초생활수급자": "저소득",
    "기초생활수급": "저소득",
    "기초수급": "저소득",
    "수급자": "저소득",
    "차상위계층": "저소득",
    "차상위": "저소득",
    "생계급여": "저소득",
    "의료급여": "저소득",
    "주거급여": "저소득",
    "저소득가구": "저소득",
    "저소득층": "저소득",
    "저소득": "저소득",

    # 장애인
    "장애인": "장애인",
    "장애아동": "장애인",
    "장애가족": "장애인",
    "중증장애": "장애인",
    "장애우": "장애인",

    # 한부모·조손
    "한부모": "한부모·조손",
    "한부모가정": "한부모·조손",
    "한부모가족": "한부모·조손",
    "조손": "한부모·조손",
    "조손가정": "한부모·조손",
    "조손가족": "한부모·조손",

    # 다문화·탈북민
    "다문화": "다문화·탈북민",
    "다문화가정": "다문화·탈북민",
    "다문화가족": "다문화·탈북민",
    "탈북민": "다문화·탈북민",
    "북한이탈주민": "다문화·탈북민",

    # 다자녀
    "다자녀": "다자녀",
    "다자녀가정": "다자녀",
    "다자녀가구": "다자녀",
    "세자녀": "다자녀",

    # 보훈대상자 (구체적 신호 우선 — 동률 길이 시 dict 순서가 tie-breaker)
    "참전유공자": "보훈대상자",
    "국가유공자": "보훈대상자",
    "보훈대상자": "보훈대상자",
    "보훈가족": "보훈대상자",
    "유공자": "보훈대상자",
    "보훈": "보훈대상자",
}


# 정규화 DB값 → 동의어 토큰 리스트 (OKMS 검색에서 BUSINESS_NAME_KO/TEXT_CHUNK_KO
# 추가 OR-부스팅에만 사용; HSHD_STTN_NM 필터값과는 별개).
# 토큰 수는 Mariner 트리 폭증 방지를 위해 의도적으로 최소화(그룹당 ≤ 5).
# 주의: 여기 들어가는 토큰은 vector/keyword 문자열에 합치지 않고 별도 WhereSet 으로만
# 전달되므로 _extract_business_anchors 가 anchor 로 승격할 위험이 없음.
_HSHD_STTN_SYNONYM_GROUPS: Dict[str, List[str]] = {
    "저소득": ["기초생활수급자", "차상위", "생계급여", "의료급여", "주거급여"],
    "장애인": ["장애아동", "중증장애"],
    "한부모·조손": ["한부모가정", "조손가정"],
    "다문화·탈북민": ["다문화가정", "북한이탈주민"],
    "다자녀": ["다자녀가정"],
    "보훈대상자": ["국가유공자", "참전유공자", "보훈가족"],
}


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


def _extract_hshd_sttn_from_message(message: str) -> Tuple[str, List[str]]:
    """메시지에서 가구상황을 추출해 (정규화 DB값, 동의어 토큰 리스트) 튜플을 반환.

    매칭은 긴 키워드 우선(예: "기초생활수급자" → "수급자"보다 먼저 매칭). 동일
    길이일 때는 dict 선언 순서가 tie-breaker (수급자 계열 우선). 매칭 없으면
    기본값 ("일반가구", [...]) 반환 (빈 message 는 ("", []) — 필터 미적용).

    동의어 리스트는 OKMS BUSINESS_NAME/TEXT_CHUNK 부스팅 전용. HSHD_STTN_NM 필터값
    자체는 정규화값(첫 번째 반환) 하나만 사용한다.
    """
    if not message:
        return "", []
    for keyword in sorted(_HSHD_STTN_KEYWORD_MAP.keys(), key=len, reverse=True):
        if keyword in message:
            norm = _HSHD_STTN_KEYWORD_MAP[keyword]
            return norm, list(_HSHD_STTN_SYNONYM_GROUPS.get(norm, []))
    # 가구상황 키워드 미검출 시 기본값 "일반가구" — 특정계층(저소득/다문화 등) 신호가
    # 없는 일반 질문이 특정계층 전용 제도로 쏠리지 않도록 일반가구 가구상황을 적용한다.
    return "일반가구", list(_HSHD_STTN_SYNONYM_GROUPS.get("일반가구", []))


def _birth_year_to_lifecycle(birth_year: int, current_year: int = None) -> str:
    """출생연도를 생애주기 라벨로 변환합니다."""
    if current_year is None:
        current_year = datetime.now().year
    age = current_year - birth_year
    if age <= 5:
        return "영유아"
    elif age <= 12:
        return "아동"
    elif age < 18:
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
