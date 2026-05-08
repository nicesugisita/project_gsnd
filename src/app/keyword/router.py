from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import logging
import time

from app.shared.utils.keyword_extractor import extract_nouns

router = APIRouter()
logger = logging.getLogger(__name__)


class KeywordExtractRequest(BaseModel):
    text: str = Field(..., description="키워드를 추출할 텍스트")
    min_length: int = Field(default=2, ge=1, description="최소 명사 길이")


@router.post("/v1/keywords/extract")
async def extract_keywords(request: KeywordExtractRequest):
    """텍스트에서 명사(키워드)를 추출합니다. (NNG: 일반명사, NNP: 고유명사, SL: 외국어)"""
    try:
        start = time.perf_counter()
        keywords = extract_nouns(request.text, request.min_length)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        return JSONResponse(
            content={"keywords": keywords, "elapsed_ms": elapsed_ms},
            status_code=200,
        )
    except Exception as e:
        logger.error(f"[KeywordExtract] 오류: {e}", exc_info=True)
        return JSONResponse(content={"detail": str(e)}, status_code=500)
