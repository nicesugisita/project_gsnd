"""
Router Service - Query Routing and Classification

사용자 질문을 분석하여 적절한 RAG 전략을 결정하고,
쿼리를 최적화하는 라우팅 서비스입니다.

주요 기능:
- 쿼리 재구성 (Query Reform)
- 쿼리 확장 (Query Expansion)
- 트리플 추출 (Triple Extraction)
- QA 유형 판단 (QA Type Query)
- 컬렉션 카테고리 선택 (Collection Category Selection)
"""

import json
import logging
import re
from typing import Dict, Any, List

from core.config import Config
from core.constants import ROLE_USER
from .llm_service import call_llm_api
from .more_results_service import is_more_results_intent
from .deepserver_service import (
    deepserver_reform_query,
    deepserver_expand_query,
    deepserver_extract_comparison_attributes,
    deepserver_extract_comparison_triples,
    _ds_post,
)
from utils.prompt_loader import load_query_recreation_prompt
from utils.helpers import shorten_text
from core.constants import ROLE_ASSISTANT

logger = logging.getLogger(__name__)


def _get_base_user_query(messages: list) -> str:
    """연속된 more-results 구간 이전의 원질문(최근 non-more-results user)을 찾는다."""
    for msg in reversed(messages or []):
        if msg.get("role") != ROLE_USER:
            continue
        content = str(msg.get("content", "") or "").strip()
        if not content:
            continue
        if not is_more_results_intent(content):
            return content
    return ""


async def reform_query(user_query: str, chat_messages: Any = None) -> str:
    """
    DeepServer를 사용해 사용자 질문을 문서 검색에 적합한 질의로 재구성

    Args:
        user_query: 원본 사용자 질문
        chat_messages: 대화 히스토리(선택) - 현재 미사용

    Returns:
        재구성된 질의 문자열 (실패 시 원본 반환)
    """
    try:
        result = await deepserver_reform_query(user_query)
        logger.info("[Query Reform] 재구성 결과: %s", shorten_text(result, 200))
        return result
    except Exception as e:
        logger.error("[Query Reform] 오류: %s", e)
        return user_query


async def query_recreation(
    initial_query: str,
    messages: list
) -> str:
    """
    최초 질문과 대화 이력을 바탕으로 완성된 검색 질의 재구성

    Args:
        initial_query: 사용자의 최초 질문
        messages: 전체 대화 이력 (되묻기 포함)

    Returns:
        재완성된 완성 질의 (실패 또는 무효한 답변 시 빈 문자열)
    """
    try:
        from datetime import date as _date
        recreation_prompt = load_query_recreation_prompt()
        if not recreation_prompt:
            logger.warning("[Query Recreation] 프롬프트 로드 실패")
            return ""
        _today = _date.today()
        recreation_prompt = (
            recreation_prompt
            .replace("{현재연도}", str(_today.year))
            .replace("{작년연도}", str(_today.year - 1))
        )

        # 입력 데이터 JSON 구성 (프롬프트의 initial_query / previous_messages 키와 일치)
        input_data = {
            "initial_query": initial_query,
            "previous_messages": messages
        }
        llm_messages = [
            {"role": ROLE_USER, "content": json.dumps(input_data, ensure_ascii=False)}
        ]
        logger.info(f"[query_recreation] initial_query: {initial_query}")
        logger.info(f"[query_recreation] messages: {messages}")
        response = await call_llm_api(
            temperature=0,
            messages=llm_messages,
            extra_system_prompts=[recreation_prompt],
            response_format={"type": "json_object"},
            api_url=Config.LLM_API_URL
        )

        # # 응답 검증
        # if not response or response.strip() == "":
        #     logger.info("[Query Recreation] 빈 응답 (조건 불만족)")
        #     return ""
        
        final_query = ""
        try:
            parsed = json.loads(response)
            rq = parsed.get("final_query") if isinstance(parsed, dict) else None
            if isinstance(rq, str):
                final_query = rq.strip()
        except Exception:
            final_query = response.strip()
        
        # # 기본 검증: 최소 2개 단어 이상
        # if len(final_query.split()) < 2:
        #     logger.info("[Query Recreation] 너무 짧은 응답: %s", final_query)
        #     return ""
        
        logger.info("[Query Recreation] 완성 질의: %s", shorten_text(final_query, 200))
        return final_query
        
    except Exception as e:
        logger.error("[Query Recreation] 오류: %s", e)
        return ""


async def classify_next_intent(messages: list, current_query: str) -> dict:
    """
    이전 대화 + 현재 질문 기반 후속 의도 분류.

    Returns:
        {"intent": "MORE_INFO" | "OTHER", "re_query": str}
    """
    from utils.prompt_loader import load_next_intent_prompt

    base_user_query = _get_base_user_query(messages)
    fallback_re_query = base_user_query or current_query
    fallback = {"intent": "OTHER", "re_query": fallback_re_query}
    try:
        prompt = load_next_intent_prompt()
        if not prompt:
            return fallback

        before_user = ""
        before_assistant = ""
        for msg in reversed(messages or []):
            role = msg.get("role")
            content = str(msg.get("content", "") or "").strip()
            if not content:
                continue
            if not before_assistant and role == ROLE_ASSISTANT:
                before_assistant = content
            elif not before_user and role == ROLE_USER:
                before_user = content
            if before_user and before_assistant:
                break

        formatted_prompt = (
            prompt
            .replace("{before_user_input}", before_user)
            .replace("{before_answer}", before_assistant)
            .replace("{user_input}", current_query)
        )

        response = await call_llm_api(
            message=formatted_prompt,
            temperature=0,
            response_format={"type": "json_object"},
            api_url=Config.LLM_API_URL,
            extra_system_prompts=[],
        )

        text = str(response or "").strip()
        if text.startswith("```"):
            text = (
                text.removeprefix("```json")
                    .removeprefix("```")
                    .removesuffix("```")
                    .strip()
            )
        match = re.search(r"\{.*\}", text, re.DOTALL)
        parsed = json.loads(match.group(0) if match else text)

        raw_intent = str(parsed.get("intent", "OTHER")).upper()
        mapped_intent = "MORE_INFO" if raw_intent == "MORE_INFO" else "OTHER"
        llm_re_query = str(parsed.get("re_query", "") or "").strip()
        re_query = llm_re_query or fallback_re_query
        if mapped_intent == "MORE_INFO" and base_user_query:
            # "더 알려줘"는 원질문 컨텍스트(예: 지역/대상)를 우선 유지한다.
            re_query = base_user_query
        result = {"intent": mapped_intent, "re_query": re_query}
        logger.info(
            "[NextIntent] intent=%s, base_query=%s, llm_re_query=%s, re_query=%s",
            mapped_intent,
            shorten_text(base_user_query, 80),
            shorten_text(llm_re_query, 80),
            shorten_text(re_query, 80),
        )
        return result
    except Exception as e:
        logger.warning("[NextIntent] 분류 실패: %s", e)
        return fallback


async def expand_query(reformed_query: str) -> list:
    """
    DeepServer를 사용해 재구성된 질의를 확장 질의로 변환

    Args:
        reformed_query: 재구성된 사용자 질문

    Returns:
        확장 질의 리스트 (실패 시 빈 리스트)
    """
    # if True:
    #     return []
    try:
        queries = await deepserver_expand_query(reformed_query)
        logger.info("[Query Expansion] 확장 성공: %d개 질의", len(queries))
        return queries
    except Exception as e:
        logger.error("[Query Expansion] 오류: %s", e)
        return []


async def extract_triples(expanded_query: str) -> list:
    """
    /rag/extract-keyword를 사용해 확장된 질의에서 키워드 추출

    Args:
        expanded_query: 확장된 질의 문장

    Returns:
        키워드 리스트 (실패 시 빈 리스트)
    """
    try:
        url = f"{Config.DEEP_SERVER_URL}/rag/extract-keyword"
        payload = {
            "QUESTION": expanded_query,
            "USER_CONV_ID": "",
            "QA_MODEL": "SLLM",
        }
        response = await _ds_post(url, payload)
        response.raise_for_status()
        data = response.json()

        if not data.get("RESULT"):
            logger.warning("[Keyword Extraction] DeepServer 실패: %s", data.get("MESSAGE"))
            return []

        keywords = data.get("OUTPUT", [])
        keywords = [k for k in keywords if k] if isinstance(keywords, list) else []
        logger.info("[Keyword Extraction] 추출 성공: %d개 키워드 %s", len(keywords), keywords)
        return keywords
    except Exception as e:
        logger.error("[Keyword Extraction] 오류: %s", e)
        return []



async def extract_comparison_attributes(query: str) -> List[str]:
    """
    DeepServer를 사용해 비교 질문에서 비교 속성 목록 추출

    Args:
        query: 비교 질문 (reformed_query)

    Returns:
        비교 속성 리스트 (예: ["지원 대상", "지원 금액"])
        실패 시 ["차이"]
    """
    try:
        attributes = await deepserver_extract_comparison_attributes(query)
        result = attributes if attributes else ["차이"]
        logger.info(f"[Comparison Attribute] 추출 결과: {result}")
        return result
    except Exception as e:
        logger.error(f"[Comparison Attribute] 오류: {e}")
        return ["차이"]


async def extract_comparison_triples(query: str) -> List[Dict[str, Any]]:
    """
    DeepServer를 사용해 비교 질문에서 검색용 트리플 추출

    Args:
        query: 비교 질문 (reformed_query)

    Returns:
        트리플 리스트 [{"Subject": str, "Predicate": str, "Object": str}, ...]
        실패 시 빈 리스트
    """
    try:
        triples = await deepserver_extract_comparison_triples(query)
        logger.info(f"[Comparison Triple] 추출 결과: {len(triples)}개 트리플")
        return triples
    except Exception as e:
        logger.error(f"[Comparison Triple] 오류: {e}")
        return []


async def select_collection_category(reformed_query: str) -> str:
    """
    재구성된 질의를 기반으로 적절한 컬렉션 카테고리 선택

    현재는 기본 컬렉션만 지원합니다.
    향후 동적 카테고리 선택 기능 추가 예정입니다.
    
    Args:
        reformed_query: 재구성된 사용자 질문
    
    Returns:
        선택된 컬렉션 카테고리 (기본값: Config.RAG_COLLECTION)
    """
    # TODO: 동적 카테고리 선택 구현
    # - LLM을 사용해 재구성된 질의로부터 카테고리 판단
    # - GSND_V1_C01~C09 중 하나를 선택
    # - 유효성 검증 후 반환
    return Config.RAG_COLLECTION
    


async def qa_type_query(user_query: str, messages: list = None) -> Dict[str, Any]:
    """
    사용자 질문을 분석하여 RAG 대상의 질의응답 유형 판단
    
    Args:
        user_query: 사용자 질문
    
    Returns:
        라우팅 결과 (JSON)
        {
        "qa_type": "FACT | STRUCTURE | JUDGMENT_EVASION"
        }
    """
    if not Config.LLM_ENABLED or not Config.LLM_API_URL:
        return {
            "rag_scope": {"is_rag_target": False, "reason": "LLM 비활성화"},
            "qa_routing": {"qa_type": "NONE", "reason": "LLM 비활성화"}
        }
    
    return {
        "rag_scope": {"is_rag_target": False, "reason": "미지원"},
        "qa_routing": {"qa_type": "NONE", "reason": "미지원"}
    }


async def process_query_pipeline(user_query: str, messages: list = None) -> Dict[str, Any]:
    """
    쿼리 처리 파이프라인: 재구성, 확장, 트리플 추출을 병렬로 처리
    
    Args:
        user_query: 사용자 질문
        messages: 대화 히스토리
    
    Returns:
        {
            "reformed_query": str,
            "expanded_queries": list[str],
            "triples": list[dict]
        }
    """
    try:
        # 1. 쿼리 재구성
        reformed_query = await reform_query(user_query, messages)
        if not reformed_query:
            reformed_query = user_query
        logger.info("[Query Pipeline] 재구성: %s", shorten_text(reformed_query, 100))

        # 2. 쿼리 확장
        expanded_queries = await expand_query(reformed_query)
        if not expanded_queries:
            logger.warning("[Query Pipeline] 쿼리 확장 실패 - 원본 사용")
            expanded_queries = [reformed_query]
        logger.info("[Query Pipeline] 확장: %d개 쿼리", len(expanded_queries))

        # 3. 확장된 질의에서 트리플 추출
        combined_query = " ".join(expanded_queries)
        triples = await extract_triples(combined_query)
        logger.info("[Query Pipeline] 트리플: %d개", len(triples))

        return {
            "reformed_query": reformed_query,
            "expanded_queries": expanded_queries,
            "triples": triples
        }
    
    except Exception as e:
        logger.error(f"[Query Pipeline] 오류: {e}", exc_info=True)
        return {
            "reformed_query": user_query,
            "expanded_queries": [user_query],
            "triples": []
        }
