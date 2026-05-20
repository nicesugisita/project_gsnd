"""
SLM(8B) 분류기 호출 + JSON 파싱 실패 시 32B 폴백 헬퍼.

배경:
- 커밋 074209b에서 4개 분류기(unified_preprocess / pre_check / next_intent / query_recreation)
  의 LLM 호출이 Config.LLM_API_URL(32B) → Config.RELEVANCE_LLM_API_URL(SLM 8B)로 이전됨.
- SLM이 가끔 깨진 JSON을 반환하거나 필수 키를 누락하는 회귀가 관찰됨
  (예: 260519 회귀 케이스 no=75, 91, 150).

전략:
- SLM 호출 후 JSON 파싱 시도.
- 파싱 실패 또는 선택적 validator가 False를 반환하면 동일 입력으로 32B에 재호출.
- 32B 결과를 반환. Config.LLM_API_URL이 비어 있으면 SLM 원본 결과 그대로 반환.

이 모듈은 call_llm_api의 얇은 래퍼이며, api_url 인자를 직접 받지 않는다 (헬퍼가 내부에서 결정).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional, Tuple

from app.core.config import Config
from .core import call_llm_api

logger = logging.getLogger(__name__)


def _strip_json_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = (
            s.removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
    return s


def _try_parse_json(text: str) -> Optional[dict]:
    """JSON 파싱 시도. 실패하면 None."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(_strip_json_fence(text))
    except json.JSONDecodeError:
        return None
    except Exception:
        return None


async def call_classifier_with_fallback(
    *,
    classifier_name: str,
    validate: Optional[Callable[[dict], bool]] = None,
    **llm_kwargs: Any,
) -> Tuple[Optional[dict], str, bool]:
    """SLM 분류기 호출 + JSON 파싱/스키마 실패 시 32B로 재호출.

    Args:
        classifier_name: 로깅용 이름 (예: "UnifiedPreprocess").
        validate: 파싱된 dict의 스키마를 검사하는 함수. None이면 파싱 성공만 통과.
        **llm_kwargs: call_llm_api에 전달할 키워드 인자 (api_url 인자는 무시됨).

    Returns:
        (parsed_dict_or_None, raw_text, used_fallback_to_32b)
        - 1차 SLM 호출이 성공하면 used_fallback_to_32b=False
        - SLM 실패 → 32B 재호출 성공/실패 모두 used_fallback_to_32b=True
    """
    llm_kwargs.pop("api_url", None)

    raw = await call_llm_api(**llm_kwargs, api_url=Config.RELEVANCE_LLM_API_URL)
    parsed = _try_parse_json(raw)
    schema_ok = bool(parsed) and (validate(parsed) if validate else True)
    if schema_ok:
        return parsed, raw, False

    fallback_url = Config.LLM_API_URL
    if not fallback_url:
        logger.warning(
            "[%s] SLM 파싱/스키마 실패했지만 LLM_API_URL이 비어있어 32B 폴백 불가",
            classifier_name,
        )
        return parsed, raw, False

    logger.warning(
        "[%s] SLM 응답 파싱/스키마 실패 → 32B 폴백 재호출 (parse_ok=%s)",
        classifier_name,
        bool(parsed),
    )
    raw2 = await call_llm_api(**llm_kwargs, api_url=fallback_url)
    parsed2 = _try_parse_json(raw2)
    return parsed2, raw2, True
