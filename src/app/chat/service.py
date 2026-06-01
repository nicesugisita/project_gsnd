"""Chat 도메인 서비스 퍼사드."""

from app.chat.preprocessing import unified_preprocess
from app.chat.sigun import (
    check_sigun,
    check_out_of_scope_region,
    count_sigun_ask_attempts,
    MSG_SIGUN_FAILURE,
    MSG_OUT_OF_SCOPE_TEMPLATE,
    MAX_SIGUN_ASK_ATTEMPTS,
)
from app.chat.lifecycle import check_lifecycle
from app.chat.query_reform import reform_query_if_needed, reform_query_with_history
from app.chat.routing import (
    expand_query,
    extract_triples,
    query_recreation,
)
from app.chat.infra.llm import (
    call_llm_api,
    convert_korean_to_standard,
    ask_judgment,
    re_ask,
    rag_norag_judgment,
    clean_query_text,
    mandatory_condition_check,
    generate_suggested_questions,
    convert_to_voice_output,
    intent_classification,
    summarize_document_text,
    classify_general_or_care,
)

__all__ = [
    "unified_preprocess",
    "check_sigun", "check_out_of_scope_region", "count_sigun_ask_attempts",
    "MSG_SIGUN_FAILURE", "MSG_OUT_OF_SCOPE_TEMPLATE", "MAX_SIGUN_ASK_ATTEMPTS",
    "check_lifecycle",
    "reform_query_with_history", "reform_query_if_needed",
    "expand_query",
    "extract_triples", "query_recreation",
    "call_llm_api", "convert_korean_to_standard", "ask_judgment", "re_ask",
    "rag_norag_judgment", "clean_query_text", "mandatory_condition_check",
    "generate_suggested_questions", "convert_to_voice_output",
    "intent_classification", "summarize_document_text", "classify_general_or_care",
]
