"""
Query processing endpoints - triple extraction, etc.
"""

import json
import logging
import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from app.core.config import Config

logger = logging.getLogger(__name__)

router = APIRouter()

DEEP_SERVER_URL = f"{Config.DEEP_SERVER_URL}/predict_rag_result"
DEEP_SERVER_REQUERY_URL = f"{Config.DEEP_SERVER_URL}/rag/re-query"
TRIPLE_EXTRACTION_PROMPT_ID = 18
QUERY_EXPAND_PROMPT_ID = 2
RE_QUERY_PROMPT_ID = 17
COMPARISON_ATTRIBUTE_PROMPT_ID = 19
COMPARISON_TRIPLE_PROMPT_ID = 20


class ExtractTriplesRequest(BaseModel):
    question: str


class ExpandQueryRequest(BaseModel):
    question: str


class ReQueryRequest(BaseModel):
    question: str
    user_conv_id: str = ""


class ComparisonAttributeRequest(BaseModel):
    question: str


class ComparisonTripleRequest(BaseModel):
    question: str


@router.post("/v1/query/extract-triples")
async def extract_triples_endpoint(request: ExtractTriplesRequest):
    """
    사용자 질문에서 트리플(Subject-Predicate-Object)을 추출합니다.
    DeepServer(/predict_rag_result)를 통해 처리합니다.
    """
    try:
        payload = {
            "REPO": "ALL",
            "QUESTION": request.question,
            "QA_MODEL": "SLLM",
            "DB_ENGINE": "MARINER",
            "TOP_N": "1",
            "DOC_ID": "",
            "THRESHOLD": "0",
            "USE_QA_WHEN_EMPTY": True,
            "MULTITURN": False,
            "USER_CONV_ID": "",
            "IS_STREAM": False,
            "IS_RAG": False,
            "PROMPT_ID": {"RE_QUERY": "1", "RAG": str(TRIPLE_EXTRACTION_PROMPT_ID)},
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(DEEP_SERVER_URL, json=payload)
            response.raise_for_status()
            data = response.json()

        if not data.get("RESULT"):
            return JSONResponse(
                content={"detail": data.get("MESSAGE", "DeepServer 오류")},
                status_code=500,
            )

        output_raw = data.get("OUTPUT", "")
        try:
            output_parsed = json.loads(output_raw)
            triples = output_parsed.get("triples", [])
        except (json.JSONDecodeError, AttributeError):
            triples = []

        return JSONResponse(content={"triples": triples}, status_code=200)

    except Exception as e:
        logger.error(f"[Extract Triples] 오류: {e}", exc_info=True)
        return JSONResponse(content={"detail": str(e)}, status_code=500)


@router.post("/v1/query/expand")
async def expand_query_endpoint(request: ExpandQueryRequest):
    """
    사용자 질문을 유사 의미의 다양한 검색 표현으로 확장합니다.
    DeepServer(/predict_rag_result)를 통해 처리합니다.
    """
    try:
        payload = {
            "REPO": "ALL",
            "QUESTION": request.question,
            "QA_MODEL": "SLLM",
            "DB_ENGINE": "MARINER",
            "TOP_N": "1",
            "DOC_ID": "",
            "THRESHOLD": "0",
            "USE_QA_WHEN_EMPTY": True,
            "MULTITURN": False,
            "USER_CONV_ID": "",
            "IS_STREAM": False,
            "IS_RAG": False,
            "PROMPT_ID": {"RE_QUERY": "1", "RAG": str(QUERY_EXPAND_PROMPT_ID)},
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(DEEP_SERVER_URL, json=payload)
            response.raise_for_status()
            data = response.json()

        if not data.get("RESULT"):
            return JSONResponse(
                content={"detail": data.get("MESSAGE", "DeepServer 오류")},
                status_code=500,
            )

        output_raw = data.get("OUTPUT", "")
        try:
            output_parsed = json.loads(output_raw)
            queries = output_parsed.get("query", [])
        except (json.JSONDecodeError, AttributeError):
            queries = []

        return JSONResponse(content={"query": queries}, status_code=200)

    except Exception as e:
        logger.error(f"[Expand Query] 오류: {e}", exc_info=True)
        return JSONResponse(content={"detail": str(e)}, status_code=500)


@router.post("/v1/query/re-query")
async def re_query_endpoint(request: ReQueryRequest):
    """
    사용자 질문을 검색에 적합한 형태로 재구성합니다.
    DeepServer(/rag/re-query)를 통해 처리합니다.
    """
    try:
        payload = {
            "QUESTION": request.question,
            "QA_MODEL": "SLLM",
            "USER_CONV_ID": request.user_conv_id,
            "PROMPT_ID": RE_QUERY_PROMPT_ID,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(DEEP_SERVER_REQUERY_URL, json=payload)
            response.raise_for_status()
            data = response.json()

        if not data.get("RESULT"):
            return JSONResponse(
                content={"detail": data.get("MESSAGE", "DeepServer 오류")},
                status_code=500,
            )

        return JSONResponse(content={"output": data.get("OUTPUT", "")}, status_code=200)

    except Exception as e:
        logger.error(f"[Re-Query] 오류: {e}", exc_info=True)
        return JSONResponse(content={"detail": str(e)}, status_code=500)


@router.post("/v1/query/comparison-attributes")
async def comparison_attributes_endpoint(request: ComparisonAttributeRequest):
    """
    intent=comparison일 경우 비교 속성을 추출합니다.
    DeepServer(/predict_rag_result)를 통해 처리합니다.
    """
    try:
        payload = {
            "REPO": "ALL",
            "QUESTION": request.question,
            "QA_MODEL": "SLLM",
            "DB_ENGINE": "MARINER",
            "TOP_N": "1",
            "DOC_ID": "",
            "THRESHOLD": "0",
            "USE_QA_WHEN_EMPTY": True,
            "MULTITURN": False,
            "USER_CONV_ID": "",
            "IS_STREAM": False,
            "IS_RAG": False,
            "PROMPT_ID": {"RE_QUERY": "1", "RAG": str(COMPARISON_ATTRIBUTE_PROMPT_ID)},
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(DEEP_SERVER_URL, json=payload)
            response.raise_for_status()
            data = response.json()

        if not data.get("RESULT"):
            return JSONResponse(
                content={"detail": data.get("MESSAGE", "DeepServer 오류")},
                status_code=500,
            )

        output_raw = data.get("OUTPUT", "")
        try:
            attributes = json.loads(output_raw)
            if not isinstance(attributes, list):
                attributes = []
        except (json.JSONDecodeError, AttributeError):
            attributes = []

        return JSONResponse(content={"attributes": attributes}, status_code=200)

    except Exception as e:
        logger.error(f"[Comparison Attributes] 오류: {e}", exc_info=True)
        return JSONResponse(content={"detail": str(e)}, status_code=500)


@router.post("/v1/query/comparison-triples")
async def comparison_triples_endpoint(request: ComparisonTripleRequest):
    """
    intent=comparison일 경우 비교 트리플(Subject-Predicate-Object)을 추출합니다.
    DeepServer(/predict_rag_result)를 통해 처리합니다.
    """
    try:
        payload = {
            "REPO": "ALL",
            "QUESTION": request.question,
            "QA_MODEL": "SLLM",
            "DB_ENGINE": "MARINER",
            "TOP_N": "1",
            "DOC_ID": "",
            "THRESHOLD": "0",
            "USE_QA_WHEN_EMPTY": True,
            "MULTITURN": False,
            "USER_CONV_ID": "",
            "IS_STREAM": False,
            "IS_RAG": False,
            "PROMPT_ID": {"RE_QUERY": "1", "RAG": str(COMPARISON_TRIPLE_PROMPT_ID)},
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(DEEP_SERVER_URL, json=payload)
            response.raise_for_status()
            data = response.json()

        if not data.get("RESULT"):
            return JSONResponse(
                content={"detail": data.get("MESSAGE", "DeepServer 오류")},
                status_code=500,
            )

        output_raw = data.get("OUTPUT", "")
        try:
            output_parsed = json.loads(output_raw)
            triples = output_parsed.get("triples", [])
        except (json.JSONDecodeError, AttributeError):
            triples = []

        return JSONResponse(content={"triples": triples}, status_code=200)

    except Exception as e:
        logger.error(f"[Comparison Triples] 오류: {e}", exc_info=True)
        return JSONResponse(content={"detail": str(e)}, status_code=500)
