"""
LLM 서비스 패키지

기존 services.llm_service import 경로와 완전 호환됩니다.
"""

# client
from .client import (
    _llm_client,
    _LLM_POOL_LIMITS,
    _POOL_RETRY_ERRORS,
    _get_llm_client,
    _reset_llm_client,
)

# core
from .core import (
    _build_messages,
    _build_payload,
    _truncate_for_context,
    _compact_payload_for_context_retry,
    _is_context_length_error,
    _call_llm_api_sync,
    _stream_llm_response,
    call_llm_api,
)

# preprocessing
from .preprocessing import (
    classify_general_or_care,
    clean_query_text,
    convert_to_voice_output,
    convert_korean_to_standard,
)

# judgment
from .judgment import (
    ask_judgment,
    mandatory_condition_check,
    re_ask,
    rag_norag_judgment,
    generate_suggested_questions,
    _split_document_text_for_summary,
    summarize_document_text,
    intent_classification,
)

# routers/tts.py 가 services.llm_service 를 통해 접근하는 prompt_loader 심볼 호환
from app.shared.utils.prompt_loader import load_voice_print_before_prompt

__all__ = [
    # client
    "_llm_client", "_LLM_POOL_LIMITS", "_POOL_RETRY_ERRORS",
    "_get_llm_client", "_reset_llm_client",
    # core
    "_build_messages", "_build_payload",
    "_truncate_for_context", "_compact_payload_for_context_retry", "_is_context_length_error",
    "_call_llm_api_sync", "_stream_llm_response",
    "call_llm_api",
    # preprocessing
    "classify_general_or_care", "clean_query_text",
    "convert_to_voice_output", "convert_korean_to_standard",
    # judgment
    "ask_judgment", "mandatory_condition_check", "re_ask",
    "rag_norag_judgment", "generate_suggested_questions",
    "_split_document_text_for_summary", "summarize_document_text",
    "intent_classification",
    # compat
    "load_voice_print_before_prompt",
]
