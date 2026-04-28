"""MORE_INFO 히스토리 기반으로 제외 chunk/service 정보를 추출합니다."""

from typing import Any, Dict, List, Optional, Tuple

from app.core.constants import ROLE_ASSISTANT


def _assistant_more_info(msg: Dict[str, Any]) -> Optional[bool]:
    """assistant 턴이 MORE_INFO(이전 결과에 이어 '더 보기' 응답)인지.

    RAG strategy intent(preprocess.intent)은 guide_recommend 등이며 MORE_INFO가 아님.
    streaming에서만 저장하는 preprocess.more_info=True 일 때 True.
    """
    preprocess = msg.get("preprocess")
    if isinstance(preprocess, dict) and "more_info" in preprocess:
        return bool(preprocess.get("more_info"))

    metadata = msg.get("metadata")
    if isinstance(metadata, dict) and "more_info" in metadata:
        return bool(metadata.get("more_info"))

    # 과거/프론트 메시지(예: metadata.referenced_documents만 있는 경우)는 판정 불가.
    return None


def _extract_references(msg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """assistant 메시지에서 chunk_id/name 목록 추출 (신규/구형 스키마 모두 지원)."""
    chunk_ids: List[str] = []
    service_names: List[str] = []

    for chunk_id in msg.get("referenced_chunk_ids", []):
        value = str(chunk_id).strip()
        if value:
            chunk_ids.append(value)

    for name in msg.get("referenced_service_names", []):
        value = str(name).strip()
        if value:
            service_names.append(value)

    metadata = msg.get("metadata")
    docs = metadata.get("referenced_documents", []) if isinstance(metadata, dict) else []
    if isinstance(docs, list):
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            cid = str(doc.get("chunk_id", "") or "").strip()
            name = str(doc.get("name", "") or "").strip()
            if cid:
                chunk_ids.append(cid)
            if name:
                service_names.append(name)

    return chunk_ids, service_names


def get_excluded_info_from_history(messages: list) -> Tuple[List[str], List[str]]:
    """대화 이력에서 assistant 응답의 chunk_ids와 서비스명을 추출.

    동작 규칙:
    - 직전 턴부터 역순으로, 연속한 MORE_INFO 응답(preprocess.more_info=True)과
      그에 앞선 '첫 답변'(more_info=False)에 해당하는 assistant의 참조를 누적
    - 원 질문 user(첫 답변이 나온 직전 user)에 도달하면 중단(그 이전은 다른 주제로 간주)

    Returns:
        (excluded_chunk_ids, excluded_service_names)
    """
    excluded_chunk_ids: List[str] = []
    excluded_service_names: List[str] = []
    seen_chunk_ids = set()
    seen_names = set()

    first_in_reverse = True
    saw_assistant = False
    latest_minfo: Optional[bool] = None

    for msg in reversed(messages or []):
        role = msg.get("role")
        if role == ROLE_ASSISTANT:
            first_in_reverse = False
            saw_assistant = True
            latest_minfo = _assistant_more_info(msg)
            ref_chunk_ids, ref_service_names = _extract_references(msg)
            for chunk_id in ref_chunk_ids:
                if not chunk_id or chunk_id in seen_chunk_ids:
                    continue
                seen_chunk_ids.add(chunk_id)
                excluded_chunk_ids.append(chunk_id)

            for name in ref_service_names:
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
                excluded_service_names.append(name)
        elif role == "user":
            content = str(msg.get("content", "") or "").strip()
            if first_in_reverse:
                first_in_reverse = False
                continue
            if not saw_assistant:
                continue
            if content and latest_minfo is False:
                break

    return excluded_chunk_ids, excluded_service_names


def get_base_user_query_from_history(messages: list) -> str:
    """연속 MORE_INFO 구간의 기준이 되는 최근 '원질문' user 본문."""
    first_in_reverse = True
    saw_assistant = False
    latest_minfo: Optional[bool] = None
    for msg in reversed(messages or []):
        role = msg.get("role")
        if role == ROLE_ASSISTANT:
            first_in_reverse = False
            saw_assistant = True
            latest_minfo = _assistant_more_info(msg)
            continue
        if role != "user":
            continue
        content = str(msg.get("content", "") or "").strip()
        if not content:
            continue
        if first_in_reverse:
            first_in_reverse = False
            continue
        if not saw_assistant:
            continue
        if latest_minfo is False:
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
