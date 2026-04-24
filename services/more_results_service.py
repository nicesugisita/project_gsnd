"""사용자가 추가 결과를 요청하는지 감지하고, 이전에 보여준 문서의 chunk_id와 서비스명을 추출합니다."""

import re
from typing import Any, Dict, List, Optional, Tuple
from core.constants import ROLE_ASSISTANT

MORE_RESULTS_PATTERNS = (
    "더 알려줘",
    "더 보여줘",
    "더 있나요",
    "더 있어요",
    "추가로 알려줘",
    "다른 것도",
    "또 있나요",
    "더 알고 싶어",
    "더 알고싶어",
    "다른 서비스",
    "또 다른",
    "그 외에",
    "더 없나요",
    "다른 것 없어",
    "다른거 없어",
    "다른 건 없어",
    "다른거 없나",
    "다른 건 없나",
)


def _normalize_more_results_text(text: str) -> str:
    """패턴 매칭용 정규화: 공백/문장부호 제거."""
    value = str(text or "").strip().lower()
    if not value:
        return ""
    return re.sub(r"[\s\?\!\.\,\~\"'`]+", "", value)


def is_more_results_intent(user_message: str) -> bool:
    """사용자가 추가 결과를 요청하는지 감지."""
    msg = _normalize_more_results_text(user_message)
    if not msg:
        return False
    return any(_normalize_more_results_text(p) in msg for p in MORE_RESULTS_PATTERNS)


def get_excluded_info_from_history(messages: list) -> Tuple[List[str], List[str]]:
    """대화 이력에서 assistant 응답의 chunk_ids와 서비스명을 추출.

    동작 규칙:
    - "더 알려줘"류 후속 요청이 연속된 구간에서만 누적 제외
    - 일반 신규 질문이 나오면 누적 구간을 끊고 제외 목록을 리셋

    Returns:
        (excluded_chunk_ids, excluded_service_names)
    """
    excluded_chunk_ids: List[str] = []
    excluded_service_names: List[str] = []
    seen_chunk_ids = set()
    seen_names = set()

    # 최근 턴부터 역순으로 훑다가, "더 알려줘"류가 아닌 사용자 질문을 만나면
    # 그 이전 이력은 새로운 주제로 간주하고 누적을 중단한다.
    for msg in reversed(messages):
        role = msg.get("role")
        if role == ROLE_ASSISTANT:
            for chunk_id in msg.get("referenced_chunk_ids", []):
                chunk_id = str(chunk_id).strip()
                if not chunk_id or chunk_id in seen_chunk_ids:
                    continue
                seen_chunk_ids.add(chunk_id)
                excluded_chunk_ids.append(chunk_id)

            for name in msg.get("referenced_service_names", []):
                name = str(name).strip()
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
                excluded_service_names.append(name)
        elif role == "user":
            content = str(msg.get("content", "") or "").strip()
            if content and not is_more_results_intent(content):
                break

    return excluded_chunk_ids, excluded_service_names


def get_base_user_query_from_history(messages: list) -> str:
    """연속 more-results 구간의 기준이 되는 최근 원질문(non-more-results user 질문) 반환."""
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        content = str(msg.get("content", "") or "").strip()
        if not content:
            continue
        if not is_more_results_intent(content):
            return content
    return ""


def get_last_preprocess_from_history(messages: list) -> Optional[Dict[str, Any]]:
    """히스토리에서 가장 최근 assistant의 전처리 메타를 반환."""
    for msg in reversed(messages or []):
        if msg.get("role") != ROLE_ASSISTANT:
            continue
        preprocess = msg.get("preprocess")
        if not isinstance(preprocess, dict):
            continue
        intent = str(preprocess.get("intent", "") or "").strip()
        reformed_query = str(preprocess.get("reformed_query", "") or "").strip()
        query = str(preprocess.get("query", "") or "").strip()
        expanded_queries = preprocess.get("expanded_queries")
        if not intent or not reformed_query:
            continue
        if not isinstance(expanded_queries, list):
            expanded_queries = []
        return {
            "query": query or reformed_query,
            "intent": intent,
            "reformed_query": reformed_query,
            "expanded_queries": [str(q).strip() for q in expanded_queries if str(q).strip()],
        }
    return None
