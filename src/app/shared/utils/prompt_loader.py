"""
Prompt loader utility module.

Handles loading prompt templates from files with caching and error handling.
mtime 기반 캐시 무효화: 디스크의 파일이 변경되면 재로딩한다.
uvicorn --reload는 .py만 감시하므로, .txt 변경 시 lru_cache가 옛 내용을 고정하는 문제를 방지.
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

# 한국은 DST가 없으므로 UTC+9 고정. zoneinfo/tzdata 의존을 피해 Windows 호환.
_KST = timezone(timedelta(hours=9))
_KOREAN_WEEKDAYS = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")


def _today_kst_placeholders() -> Dict[str, str]:
    """system_prompt.txt 의 날짜 자리표시자를 KST 기준 오늘 값으로 채운다."""
    now = datetime.now(_KST)
    return {
        "{오늘날짜}": now.strftime("%Y-%m-%d"),
        "{오늘요일}": _KOREAN_WEEKDAYS[now.weekday()],
        "{현재연도}": str(now.year),
    }

# prompt_loader.py: src/app/shared/utils/ → 4단계 상위가 프로젝트 루트
_PROMPT_BASE_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..', '..', '..', '..', 'prompts'
))

# 캐시 엔트리: filename → (mtime_ns, content)
_PROMPT_CACHE: Dict[str, Tuple[int, str]] = {}


def _load_prompt_file(filename: str, default: str = "") -> str:
    """
    Load prompt from file with mtime-based cache invalidation.

    Args:
        filename: Name of the prompt file
        default: Default value if file not found

    Returns:
        Content of the prompt file or default value

    Note:
        Config.USE_SHORT_PROMPTS=True 이면 prompts/short/<filename> 가
        존재할 때 그것을 우선 사용한다. 응답시간 실험용 토글.
    """
    try:
        prompt_file = os.path.join(_PROMPT_BASE_DIR, filename)

        # 단축 프롬프트 우선 로드 (실험용 토글, 실패 시 일반 경로로 폴백)
        try:
            from app.core.config import Config
            if getattr(Config, "USE_SHORT_PROMPTS", False):
                short_file = os.path.join(_PROMPT_BASE_DIR, "short", filename)
                if os.path.exists(short_file):
                    prompt_file = short_file
        except Exception:
            pass

        if not os.path.exists(prompt_file):
            return default

        mtime_ns = os.stat(prompt_file).st_mtime_ns
        cached = _PROMPT_CACHE.get(filename)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]

        with open(prompt_file, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        _PROMPT_CACHE[filename] = (mtime_ns, content)
        logger.info(f"Loaded prompt (mtime={mtime_ns}): {filename}")
        return content

    except Exception as e:
        logger.error(f"Error loading prompt {filename}: {e}")
        return default


def load_system_prompt() -> str:
    """Load system prompt from file.

    `{오늘날짜}`, `{오늘요일}`, `{현재연도}` 자리표시자는 KST 기준으로 매 호출 치환한다.
    (캐시는 파일 내용 자체에만 적용되므로 날짜 자리표시자가 캐시되어 굳지 않는다.)
    """
    default_prompt = "당신은 경상남도청의 AI 어시스턴트입니다."
    content = _load_prompt_file('system_prompt.txt', default_prompt)
    for placeholder, value in _today_kst_placeholders().items():
        content = content.replace(placeholder, value)
    return content


def load_query_reform_prompt() -> str:
    """Load Query Reform prompt from file."""
    return _load_prompt_file('query_reform_prompt.txt')


def load_query_expansion_prompt() -> str:
    """Load Query Expansion prompt from file."""
    return _load_prompt_file('query_expansion_prompt.txt')


def load_triple_extraction_prompt() -> str:
    """Load Triple Extraction prompt from file."""
    return _load_prompt_file('triple_extraction_prompt.txt')


def load_convert_korean_prompt() -> str:
    """Load Convert Korean prompt from file."""
    return _load_prompt_file('convert_korean_prompt.txt')


def load_ask_judgment_prompt() -> str:
    """Load Ask Judgment prompt from file."""
    return _load_prompt_file('ask_judgment_prompt.txt')


def load_re_ask_prompt() -> str:
    """Load Re-ask prompt from file."""
    return _load_prompt_file('re_ask_prompt.txt')


def load_rag_norag_judgment_prompt() -> str:
    """Load RAG/NO-RAG Judgment prompt from file."""
    return _load_prompt_file('rag_norag_judgment_prompt.txt')


def load_text_cleaning_prompt() -> str:
    """Load Text Cleaning prompt from file."""
    return _load_prompt_file('text_cleaning_prompt.txt')


def load_voice_cleaning_prompt() -> str:
    """Load Voice Cleaning prompt from file."""
    return _load_prompt_file('voice_cleaning_prompt.txt')


def load_final_response_prompt() -> str:
    """Load Final Response prompt from file."""
    return _load_prompt_file('final_response_prompt.txt')

def load_general_or_care_prompt() -> str:
    """Load General or Care intent classification prompt from file."""
    return _load_prompt_file('general_or_care_prompt.txt')


def load_query_recreation_prompt() -> str:
    """Load Query Recreation prompt from file."""
    return _load_prompt_file('query_recreation_prompt.txt')


def load_suggest_questions_prompt() -> str:
    """Load Suggest Questions prompt from file."""
    return _load_prompt_file('suggest_questions_prompt.txt')


def load_voice_print_before_prompt() -> str:
    """Load Voice Print Before prompt from file."""
    return _load_prompt_file('voice_print_before_prompt.txt')


def load_intent_classification_prompt() -> str:
    """Load Intent Classification prompt from file."""
    return _load_prompt_file('intent_classification_prompt.txt')


def load_classification_general_prompt() -> str:
    """Load Classification General prompt from file."""
    return _load_prompt_file('classification_general_prompt.txt')


def load_classification_comparison_prompt() -> str:
    """Load Classification Comparison prompt from file."""
    return _load_prompt_file('classification_comparison_prompt.txt')


def load_comparison_extract_prompt() -> str:
    """Load Comparison Extract prompt from file."""
    return _load_prompt_file('comparison_extract_prompt.txt')


def load_comparison_attribute_prompt() -> str:
    """Load Comparison Attribute extraction prompt from file."""
    return _load_prompt_file('comparison_attribute_prompt.txt')


def load_comparison_triple_prompt() -> str:
    """Load Comparison Triple extraction prompt from file."""
    return _load_prompt_file('comparison_triple_prompt.txt')


def load_classification_recommended_prompt() -> str:
    """Load Classification Recommended prompt from file."""
    return _load_prompt_file('classification_recommended_prompt.txt')


def load_classification_recommended_select_prompt() -> str:
    """Load Classification Recommended *선별* prompt (LLM relevance-select 모드) from file.

    classification_recommended_prompt.txt(전수 안내)와 달리, retrieved_documents 중
    user_query와 관련된 사업만 선별 출력하도록 지시한다.
    """
    return _load_prompt_file('classification_recommended_select_prompt.txt')


def load_classification_llm_recommended_prompt() -> str:
    """Load LLM-style recommended-question final answer prompt from file."""
    return _load_prompt_file('classification_llm_recommended_prompt.txt')


def load_classification_search_prompt() -> str:
    """Load Classification Search prompt from file."""
    return _load_prompt_file('classification_search_prompt.txt')


def load_region_age_collect_recommended_prompt() -> str:
    """Load Region/Age Collect Recommended prompt from file."""
    return _load_prompt_file('region_age_collect_recommended_prompt.txt')


def load_document_summary_prompt() -> str:
    """Load Document Summary prompt from file."""
    return _load_prompt_file('document_summary_prompt.txt')


def load_uploaded_qa_prompt() -> str:
    """Load Uploaded QA prompt from file."""
    default_prompt = (
        "당신은 업로드 문서 전용 질의응답 도우미입니다. "
        "반드시 제공된 문서 내용만 근거로 답변하세요. "
        "문서에 근거가 없으면 없다고 명확히 답하세요."
    )
    return _load_prompt_file('uploaded_qa_prompt.txt', default_prompt)


def load_unified_preprocessing_prompt() -> str:
    """Load Unified Preprocessing prompt — config QUERY_REWRITING_ENABLED 에 따라 분기.

    - True : unified_preprocessing_prompt_rewrite.txt (단일 self-contained 쿼리, expansion 없음)
    - False(기본): unified_preprocessing_prompt.txt (의미 보존 expansion 5개)

    런타임 분기지만 _load_prompt_file 가 mtime 캐시를 가지므로 양쪽 모두 캐시된다.
    """
    # lazy import: Config 의존을 prompt_loader 모듈 초기화에 끌어들이지 않기 위해 지연.
    from app.core.config import Config
    if getattr(Config, "QUERY_REWRITING_ENABLED", False):
        return _load_prompt_file('unified_preprocessing_prompt_rewrite.txt')
    return _load_prompt_file('unified_preprocessing_prompt.txt')


def load_lifecycle_classification_prompt() -> str:
    """Load 생애주기 태그 분류 prompt — unified_preprocess와 분리된 단일-task 분류기."""
    return _load_prompt_file('lifecycle_classification_prompt.txt')


def load_next_intent_prompt() -> str:
    """Load next-turn intent classification prompt from file."""
    return _load_prompt_file('next_intent_prompt.txt')


def load_pre_check_prompt() -> str:
    """Load Pre-check prompt (use_rag + clarification) from file."""
    return _load_prompt_file('pre_check_prompt.txt')


def load_excluded_service_extraction_prompt() -> str:
    """Load Excluded Service Extraction prompt from file."""
    return _load_prompt_file('excluded_service_extraction_prompt.txt')


def load_contextual_query_rewriter_multi_turn_prompt() -> str:
    """Load ContextualQueryRewriter multi-turn system prompt from file."""
    return _load_prompt_file('contextual_query_rewriter_multi_turn.txt')


def load_contextual_query_rewriter_single_turn_prompt() -> str:
    """Load ContextualQueryRewriter single-turn system prompt from file."""
    return _load_prompt_file('contextual_query_rewriter_single_turn.txt')


def load_contextual_query_rewriter_output_spec() -> str:
    """Load ContextualQueryRewriter OUTPUT_SPEC (공통, 두 프롬프트에 concat) from file."""
    return _load_prompt_file('contextual_query_rewriter_output_spec.txt')
