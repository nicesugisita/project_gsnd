"""배제 사업명 추출 — 사용자 질의에서 '○○ 외/말고/제외하고' 명시된 사업명 추출.

사용자가 "의료급여 외 받을 수 있는 지원" 처럼 명시적 배제 표현을 사용했을 때,
배제 대상 사업명("의료급여")을 추출하여 최종 답변에서 해당 사업을 제외하는 데 사용.

매칭은 `app.chat.infra.rag.common.filter_excluded_docs` 의 NAME/BUSINESS_NAME
정확일치로 동작. 정확일치 한계는 사용자 합의된 사항(refer: plan).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.chat.infra.llm.classifier_fallback import call_classifier_with_fallback
from app.shared.utils.prompt_loader import load_excluded_service_extraction_prompt

logger = logging.getLogger(__name__)


def _normalize_excluded_services(parsed: Dict[str, Any]) -> List[str]:
    """LLM 응답에서 excluded_services 리스트 정규화.

    - 스네이크/카멜 케이스 모두 허용
    - None/null/문자열 단일 값도 허용 (안전 폴백)
    - 문자열 strip, 빈 문자열 제거, 중복 제거
    """
    raw = None
    for key in ("excluded_services", "excludedServices"):
        if key in parsed and parsed[key] is not None:
            raw = parsed[key]
            break
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        logger.warning("[ExcludedServiceExtract] 예상치 못한 타입: %r → []", type(raw).__name__)
        return []
    seen: set[str] = set()
    result: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        result.append(s)
    return result


async def extract_excluded_services(
    user_query: str,
    messages: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """배제 대상 사업명을 LLM으로 추출.

    실패·추출 없음 시 빈 리스트 반환 — 답변 차단 사유로 사용되지 않음.

    Args:
        user_query: 사용자 입력
        messages: 대화 이력 (현재는 단발 추출만 — 멀티턴 컨텍스트는 미사용)

    Returns:
        배제 사업명 리스트 (예: ["의료급여"]). 추출 없으면 [].
    """
    prompt_template = load_excluded_service_extraction_prompt()
    if not prompt_template:
        logger.error("[ExcludedServiceExtract] 프롬프트 로드 실패 → 빈 리스트 반환")
        return []

    final_prompt = prompt_template.replace("{사용자 질문}", user_query or "")

    try:
        parsed, _raw, used_32b = await call_classifier_with_fallback(
            classifier_name="ExcludedServiceExtract",
            message=final_prompt,
            temperature=0,
            response_format={"type": "json_object"},
            extra_system_prompts=[],
        )
    except Exception as e:
        logger.warning("[ExcludedServiceExtract] LLM 호출 실패: %s → 빈 리스트", e)
        return []

    if parsed is None:
        logger.info("[ExcludedServiceExtract] SLM/32B 양쪽 JSON 파싱 실패 → 빈 리스트 (used_32b=%s)", used_32b)
        return []

    excluded = _normalize_excluded_services(parsed)
    if excluded:
        logger.info(
            "[ExcludedServiceExtract] 배제 사업 추출: %s | used_32b=%s | query=%s",
            excluded, used_32b, (user_query or "")[:60],
        )
    return excluded
