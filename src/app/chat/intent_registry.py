"""Intent 라우팅 단일 진실 공급원(SSOT).

P3 리팩토링 산출물 — 기존에 코드 곳곳에 흩어져 있던 `if intent == "..."` 분기를
intent 메타데이터 테이블로 통합한다.

설계 원칙
- **데이터로 분기 표현**: 어떤 intent가 어떤 processor·옵션을 갖는지를
  `IntentSpec` 데이터로 표현. 새 intent 추가 시 본 파일만 변경.
- **런타임 안정성**: 알 수 없는 intent는 항상 fallback(general)으로 안전 변환.
- **YAGNI**: 실 수요가 있는 메타데이터만 필드로 노출.
- **import 사이클 회피**: processor 함수는 lazy resolve 형태로 보관 (Callable).

본 모듈은 _helpers·_streaming·_pipeline_steps·router·preprocessing 가 공통으로 참조한다.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# IntentSpec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntentSpec:
    """단일 intent 의 라우팅·동작 메타데이터.

    필드 의미:
      name                  — intent 라벨(`general`, `comparison`, ...)
      processor_provider    — RAG processor 를 lazy 로 반환하는 callable
                              (모듈 임포트 사이클 회피용)
      requires_lifecycle    — 생애주기(`run_lifecycle_check`) 실행 대상
      supports_detail_form  — `more_detail` 플래그를 RAG kwargs 에 전달
                              (form B 4단계 상세 답변 분기)
      reuse_label_on_more_info
                            — MORE_INFO 후속 발화 시 묶어 처리될 라벨
                              (현재는 모든 intent 가 `guide_recommend` 로 일원화되어
                              guide_recommend 외에는 빈 문자열)
      forced_label_reason   — `_build_preprocess_from_history` 의 intent_reason
                              로깅에 사용되는 라벨 (운영 분석용)
    """

    name: str
    processor_provider: Callable[[], Callable]
    requires_lifecycle: bool = False
    supports_detail_form: bool = False
    reuse_label_on_more_info: str = ""
    forced_label_reason: str = "reused_from_history_on_more_info"
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def processor(self) -> Callable:
        return self.processor_provider()


# ---------------------------------------------------------------------------
# Processor lazy providers
# ---------------------------------------------------------------------------
# 본 함수들은 모듈 최상위에서 import 하지 않고, 처음 호출될 때 임포트하여
# `app.chat.intent_registry → infra.rag.pipeline_* → ... → app.chat.*` 순환을 끊는다.

def _provide_general():
    from app.chat.infra.rag.pipeline_general import process_rag_general
    return process_rag_general


def _provide_comparison():
    # pipeline_comparison 의 실제 함수명은 process_rag_with_documents_v2.
    # 기존 코드에서 `as process_rag_comparison` 으로 alias 되어 사용됨.
    from app.chat.infra.rag.pipeline_comparison import (
        process_rag_with_documents_v2 as process_rag_comparison,
    )
    return process_rag_comparison


def _provide_guide_recommend():
    from app.chat.infra.rag.pipeline_guide_recommend import process_rag_guide_recommend
    return process_rag_guide_recommend


def _provide_search():
    from app.chat.infra.rag.pipeline_search import process_rag_search
    return process_rag_search


def _provide_recommended_question():
    from app.chat.infra.rag.pipeline_recommended_question import (
        process_rag_recommended_question,
    )
    return process_rag_recommended_question


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

INTENT_REGISTRY: Mapping[str, IntentSpec] = {
    "general": IntentSpec(
        name="general",
        processor_provider=_provide_general,
        supports_detail_form=True,
        forced_label_reason="reused_from_history_on_more_info",
    ),
    "comparison": IntentSpec(
        name="comparison",
        processor_provider=_provide_comparison,
    ),
    "guide_recommend": IntentSpec(
        name="guide_recommend",
        processor_provider=_provide_guide_recommend,
        requires_lifecycle=True,
        reuse_label_on_more_info="guide_recommend",
        forced_label_reason="forced_guide_recommend_on_more_info",
    ),
    "search": IntentSpec(
        name="search",
        processor_provider=_provide_search,
    ),
}

DEFAULT_INTENT_NAME = "general"

# 추천 질문 API 전용 — intent 와 직교한 별도 라우트
RECOMMENDED_QUESTION_SPEC = IntentSpec(
    name="_recommended_question",
    processor_provider=_provide_recommended_question,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_intent_names() -> tuple[str, ...]:
    """등록된 정식 intent 이름 튜플 (사용처: VALID_INTENTS)."""
    return tuple(INTENT_REGISTRY.keys())


def lookup(intent: str | None) -> IntentSpec:
    """intent 이름을 IntentSpec 으로 변환.

    알 수 없는 값은 안전하게 default(`general`) 로 폴백한다.
    """
    if intent and intent in INTENT_REGISTRY:
        return INTENT_REGISTRY[intent]
    return INTENT_REGISTRY[DEFAULT_INTENT_NAME]


def normalize_intent(intent: str | None) -> str:
    """intent 라벨을 정식 이름으로 정규화 (미등록 → default)."""
    return lookup(intent).name


# search 정의(unified_preprocessing_prompt.txt): 시설/기관 + 위치·주소·전화·연락처·홈페이지·길찾기.
# MORE_DETAIL 시 prior=search 였더라도 사용자 발화에 아래 연락처류 키워드가 없으면
# 사업/제도 디테일 요청으로 보고 general 로 좁힌다.
_FACILITY_CONTACT_KEYWORDS: tuple[str, ...] = (
    "연락처",
    "전화",
    "전화번호",
    "폰",
    "주소",
    "위치",
    "홈페이지",
    "길찾기",
    "찾아가는",
    "찾아가기",
    "오는길",
    "오는 길",
)


def _is_facility_contact_query(text: str) -> bool:
    """시설 자체의 위치/연락처를 묻는 query 인지 — search 의 정의 그대로."""
    if not text:
        return False
    haystack = str(text)
    return any(kw in haystack for kw in _FACILITY_CONTACT_KEYWORDS)


def resolve_reused_intent_on_more(
    prior_intent: str | None,
    *,
    more_detail: bool,
    user_message: str = "",
) -> str:
    """MORE_INFO/MORE_DETAIL 후속 발화 시 RAG 처리에 사용할 intent 라벨.

    기존 코드 (`_streaming.py:497~518`, `router.py:354~365`) 의 분기 로직을
    한 곳에 모은 단일 진실 공급원.

    - more_detail=False (MORE_INFO):
        guide_recommend 로 묶어 처리 (현재 디자인)
    - more_detail=True (MORE_DETAIL):
        prior_intent 가 search 이고 **현재 발화에 연락처/주소류 키워드가 있을 때만** search 유지.
        그 외(사업·제도 디테일 요청)는 general 로 좁힌다.

        배경: 이전엔 prior=search 면 무조건 search 유지였으나, 사용자가 직전 turn 이 아닌
        그 이전 turn 의 사업명을 명시해 디테일을 요청해도 search 로 가서 시설 검색을 돌리는
        버그가 있었음(예: 직전이 "행정복지센터 연락처" search, 현재가 "장애아동수당 자세히").
        search 의 정의는 시설/기관 + 위치·연락처 류이므로 그 외 디테일은 모두 general.
    """
    if not more_detail:
        return INTENT_REGISTRY["guide_recommend"].name
    prior = normalize_intent(prior_intent) if prior_intent else DEFAULT_INTENT_NAME
    if prior == "search" and _is_facility_contact_query(user_message):
        return "search"
    return DEFAULT_INTENT_NAME
