"""MORE_INFO 히스토리 기반으로 제외 chunk/service 정보를 추출합니다."""

import re
import logging
from typing import Any, Dict, List, Optional, Tuple

from app.core.constants import ROLE_ASSISTANT
from app.shared.utils.keyword_extractor import extract_nouns


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
            # latest_minfo is not True: False(첫 답변) 또는 None(메타 없는 구버전 메시지) 모두 체인 경계로 처리.
            # 단, 직전 assistant 턴에서 참조문서가 비어 있으면(예: 응답-문서 매칭 0건) 바로 끊지 않고
            # 한 단계 더 거슬러 올라가 이전 assistant의 참조를 집계한다.
            # 또한 more_info 플래그가 잘못 저장된 경우를 대비해, user 발화가 저정보 후속(예: "더 알려줘")이면
            # 체인을 유지하고 더 과거 assistant를 탐색한다.
            if (
                content
                and latest_minfo is not True
                and (excluded_chunk_ids or excluded_service_names)
                and not _is_context_dependent_followup(content)
            ):
                break

    return excluded_chunk_ids, excluded_service_names


def get_base_user_query_from_history(messages: list) -> str:
    """연속 MORE_INFO 구간의 기준이 되는 최근 '원질문' user 본문.

    NOTE:
    일부 경로에서 assistant preprocess.more_info 메타가 누락되면 latest_minfo가 None이 되어
    기준 질문을 찾지 못하고 빈 문자열이 반환될 수 있다. 그 경우 최근 user 발화와
    그 이전 user 발화를 구조적으로 구분해 기준 질문을 보강한다.
    """
    first_in_reverse = True
    saw_assistant = False
    latest_minfo: Optional[bool] = None
    user_candidates: List[str] = []
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
        user_candidates.append(content)
        if first_in_reverse:
            first_in_reverse = False
            continue
        if not saw_assistant:
            continue
        # latest_minfo is not True: False 또는 None(메타 없는 구버전) 모두 체인 경계로 처리.
        # 단, 사용자 발화가 저정보 후속이면 경계로 보지 않고 더 과거 원질문을 탐색한다.
        if latest_minfo is not True and not _is_context_dependent_followup(content):
            return content
    # fallback: more_info 메타가 누락된 경우
    # - user_candidates[0]: 가장 최근 user(대개 "더 알려줘")
    # - 그 이전 user들 중 "시군 되묻기 응답(예: 진주)"처럼 짧은 지역-only 응답은 제외하고
    #   원질문 후보를 선택한다.
    if len(user_candidates) >= 2:
        for candidate in user_candidates[1:]:
            if not _is_sigun_only_reply(candidate):
                return candidate
        return user_candidates[1]
    if user_candidates:
        # 후보가 1개뿐이면 직전 follow-up 본문일 가능성이 높아 기준 원질문으로 쓰지 않는다.
        return ""
    return ""


def _is_context_dependent_followup(content: str) -> bool:
    """명시 주제가 부족한 짧은 후속 발화인지 추정한다."""
    text = str(content or "").strip()
    if not text:
        return False
    compact = "".join(text.split())
    if len(compact) > 20:
        return False
    try:
        nouns = [n.strip() for n in extract_nouns(text, use_bigram=False) if n and n.strip()]
    except Exception:
        nouns = []
    return len(nouns) <= 1


def _is_sigun_only_reply(content: str) -> bool:
    """'진주', '창원시'처럼 시군만 단답한 사용자 입력인지 추정."""
    text = str(content or "").strip()
    if not text:
        return False
    compact = re.sub(r"\s+", "", text)
    # 단답형 지역 응답만 대상으로 제한 (일반 질문 오탐 방지)
    if len(compact) > 8:
        return False
    if not re.fullmatch(r"[가-힣]+", compact):
        return False

    try:
        # 지연 임포트: 순환 참조 방지
        from app.chat.infra.rag import _extract_sigun_from_message
        from app.mariner.sigun_utils import normalize_sigun
    except Exception:
        return False

    raws = _extract_sigun_from_message(text)
    normalized = [normalize_sigun(r) for r in raws if r and r != "경남"]
    siguns = [s for s in normalized if s.startswith("경상남도 ")]
    return len(siguns) == 1


def collect_prior_service_names(messages: list, *, max_items: int = 12) -> List[str]:
    """가장 최근 assistant 응답에서 안내된 사업명 목록(중복 제거).

    next_intent 분류기에 prior_service_names 컨텍스트로 주입하는 용도.
    `referenced_service_names`(직접 키) 또는 `metadata.referenced_documents[].name` 중 어디에 저장돼도
    하나로 합치고, 등장 순서를 유지한다.
    """
    if not messages:
        return []
    for msg in reversed(messages):
        if msg.get("role") != ROLE_ASSISTANT:
            continue
        names: List[str] = []
        seen: set[str] = set()
        for name in msg.get("referenced_service_names") or []:
            value = str(name or "").strip()
            if value and value not in seen:
                seen.add(value)
                names.append(value)
        metadata = msg.get("metadata")
        docs = metadata.get("referenced_documents", []) if isinstance(metadata, dict) else []
        if isinstance(docs, list):
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                value = str(doc.get("name", "") or "").strip()
                if value and value not in seen:
                    seen.add(value)
                    names.append(value)
        if names:
            return names[:max_items]
        return []
    return []


def get_last_preprocess_from_history(
    messages: list,
    *,
    allowed_intents: Optional[set[str]] = None,
) -> Optional[Dict[str, Any]]:
    """히스토리에서 가장 최근 assistant의 전처리 메타를 반환.

    Args:
        messages: 대화 이력
        allowed_intents: 지정 시 해당 intent 집합에 포함되는 전처리만 반환
    """
    normalized_allowed = {
        str(intent).strip()
        for intent in (allowed_intents or set())
        if str(intent).strip()
    }
    for msg in reversed(messages or []):
        if msg.get("role") != ROLE_ASSISTANT:
            continue
        preprocess = msg.get("preprocess")
        # backward-compat: some codepaths stored preprocess under metadata or as separate keys
        if not isinstance(preprocess, dict):
            metadata = msg.get("metadata")
            if isinstance(metadata, dict):
                # full preprocess dict stored in metadata
                maybe = metadata.get("preprocess")
                if isinstance(maybe, dict):
                    preprocess = maybe
                else:
                    # legacy flattened fields
                    intent = str(metadata.get("chat_intent", "") or "").strip()
                    reformed = str(metadata.get("reformed_query", "") or "").strip()
                    query = str(metadata.get("query", "") or "").strip()
                    expanded = metadata.get("expanded_queries")
                    if intent and reformed:
                        preprocess = {
                            "intent": intent,
                            "reformed_query": reformed,
                            "query": query,
                            "expanded_queries": expanded if isinstance(expanded, list) else [],
                            "search_target": metadata.get("search_target"),
                            "policy_priority_tag": metadata.get("policy_priority_tag"),
                        }
        if not isinstance(preprocess, dict):
            continue
        intent = str(preprocess.get("intent", "") or "").strip()
        reformed_query = str(preprocess.get("reformed_query", "") or "").strip()
        query = str(preprocess.get("query", "") or "").strip()
        expanded_queries = preprocess.get("expanded_queries")
        if not intent or not reformed_query:
            continue
        if normalized_allowed and intent not in normalized_allowed:
            continue
        if not isinstance(expanded_queries, list):
            expanded_queries = []
        return {
            "query": query or reformed_query,
            "intent": intent,
            "reformed_query": reformed_query,
            "expanded_queries": [str(q).strip() for q in expanded_queries if str(q).strip()],
            "search_target": preprocess.get("search_target"),
            "policy_priority_tag": preprocess.get("policy_priority_tag"),
        }
    # debug: when no preprocess found, log a brief tail of recent messages (max 20)
    try:
        logger = logging.getLogger(__name__)
        tail = list((messages or [])[-20:])
        brief = []
        for m in tail:
            try:
                role = m.get("role")
                content = str(m.get("content", "") or "")
                has_pre = isinstance(m.get("preprocess"), dict) or (
                    isinstance(m.get("metadata"), dict) and "preprocess" in m.get("metadata")
                )
                brief.append({"role": role, "has_preprocess": bool(has_pre), "content_preview": content[:200]})
            except Exception:
                brief.append({"role": None, "has_preprocess": False, "content_preview": "<serializing error>"})
        logger.debug("[MoreResults debug] get_last_preprocess_from_history -> no preprocess found; messages_tail=%s", brief)
    except Exception:
        logging.exception("Failed to log messages tail in get_last_preprocess_from_history")
    return None
