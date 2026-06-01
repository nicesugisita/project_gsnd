"""
Router Service - Query Routing and Classification

사용자 질문을 분석하여 적절한 RAG 전략을 결정하고,
쿼리를 최적화하는 라우팅 서비스입니다.

주요 기능:
- 쿼리 재구성 (Query Reform)
- 쿼리 확장 (Query Expansion)
- 트리플 추출 (Triple Extraction)
"""

import json
import logging
from typing import Dict, Any, List

from app.core.config import Config
from app.core.constants import ROLE_USER
from app.chat.infra.llm.classifier_fallback import call_classifier_with_fallback
from app.chat.infra.deepserver.client import (
    deepserver_expand_query,
    deepserver_extract_comparison_attributes,
    deepserver_extract_comparison_triples,
    _ds_post,
)
from app.chat.infra.rag.expansion_cap import dedupe_cap_expanded_queries
from app.shared.utils.prompt_loader import load_query_recreation_prompt
from app.shared.utils.helpers import shorten_text

logger = logging.getLogger(__name__)


# 키워드 → 부스팅 검색어 매핑. final_query에 키워드가 포함되어 있으면
# 매핑된 사업/제도명을 검색 질의에 추가하여 RAG 검색 시 노출 가능성을 높인다.
KEYWORD_BOOST_MAP: Dict[str, List[str]] = {
    "실직": ["긴급지원제도"],
    "병원비": ["의료비", "진료비"],  # 구어 '병원비' → 복지 DB 표준어휘 동의어 확장(OR 매칭, recall↑)
}


def _apply_keyword_boost(query: str) -> str:
    """final_query에 등록된 키워드가 있으면 매핑된 부스팅 검색어를 덧붙여 반환."""
    if not query:
        return query
    boosts: List[str] = []
    for keyword, terms in KEYWORD_BOOST_MAP.items():
        if keyword in query:
            for term in terms:
                if term and term not in query and term not in boosts:
                    boosts.append(term)
    if not boosts:
        return query
    boosted = f"{query} {' '.join(boosts)}"
    logger.info("[Query Recreation] 키워드 부스팅 적용: +%s", boosts)
    return boosted


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
        parsed, response, used_32b = await call_classifier_with_fallback(
            classifier_name="QueryRecreation",
            validate=lambda d: isinstance(d, dict) and isinstance(d.get("final_query"), str) and bool(d.get("final_query").strip()),
            temperature=0,
            messages=llm_messages,
            extra_system_prompts=[recreation_prompt],
            response_format={"type": "json_object"},
        )
        if used_32b:
            logger.info("[Query Recreation] 32B 폴백 응답 사용")

        final_query = ""
        if isinstance(parsed, dict):
            rq = parsed.get("final_query")
            if isinstance(rq, str):
                final_query = rq.strip()
        if not final_query and isinstance(response, str):
            final_query = response.strip()

        final_query = _apply_keyword_boost(final_query)
        logger.info("[Query Recreation] 완성 질의: %s", shorten_text(final_query, 200))
        return final_query
        
    except Exception as e:
        logger.error("[Query Recreation] 오류: %s", e)
        return ""


async def expand_query(reformed_query: str) -> list:
    """
    DeepServer를 사용해 재구성된 질의를 확장 질의로 변환

    Args:
        reformed_query: 재구성된 사용자 질문

    Returns:
        확장 질의 리스트 (실패 시 빈 리스트)
    """
    try:
        queries = await deepserver_expand_query(reformed_query)
        capped = dedupe_cap_expanded_queries(queries, reformed_query=reformed_query)
        logger.info(
            "[Query Expansion] 확장 성공: 원본 %d개 → 상한·중복제거 후 %d개 질의",
            len(queries or []),
            len(capped),
        )
        return capped
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




