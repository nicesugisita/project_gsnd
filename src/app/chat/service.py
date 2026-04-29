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
from app.chat.more_results import (
    get_last_preprocess_from_history,
    get_base_user_query_from_history,
    get_excluded_info_from_history,
)
from app.chat.query_reform import reform_query_with_history
from app.chat.routing import (
    classify_next_intent,
    reform_query,
    expand_query,
    extract_triples,
    select_collection_category,
    query_recreation,
)
from app.chat.retrieval_judgment import retrieval_sufficiency_judgment
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
    "get_last_preprocess_from_history", "get_base_user_query_from_history",
    "get_excluded_info_from_history",
    "reform_query_with_history",
    "classify_next_intent", "reform_query", "expand_query",
    "extract_triples", "select_collection_category", "query_recreation",
    "retrieval_sufficiency_judgment",
    "call_llm_api", "convert_korean_to_standard", "ask_judgment", "re_ask",
    "rag_norag_judgment", "clean_query_text", "mandatory_condition_check",
    "generate_suggested_questions", "convert_to_voice_output",
    "intent_classification", "summarize_document_text", "classify_general_or_care",
]
