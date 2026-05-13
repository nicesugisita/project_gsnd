"""
Prompt loader utility module.

Handles loading prompt templates from files with caching and error handling.
mtime 기반 캐시 무효화: 디스크의 파일이 변경되면 재로딩한다.
uvicorn --reload는 .py만 감시하므로, .txt 변경 시 lru_cache가 옛 내용을 고정하는 문제를 방지.
"""

import os
import logging
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

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
    """
    try:
        prompt_file = os.path.join(_PROMPT_BASE_DIR, filename)

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
    """Load system prompt from file."""
    default_prompt = "당신은 경상남도청의 AI 어시스턴트입니다."
    return _load_prompt_file('system_prompt.txt', default_prompt)


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


def load_next_intent_prompt() -> str:
    """Load next-turn intent classification prompt from file."""
    return _load_prompt_file('next_intent_prompt.txt')


def load_pre_check_prompt() -> str:
    """Load Pre-check prompt (use_rag + clarification) from file."""
    return _load_prompt_file('pre_check_prompt.txt')
