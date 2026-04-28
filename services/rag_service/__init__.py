"""RAG 서비스 패키지"""

# collection
from .collection import (
    INTENT_COLLECTION_MAP,
    _INTENT_ALIAS_MAP,
    _normalize_intent,
    _resolve_collection_for_intent,
    _uses_okms_document_schema,
    _uses_gsnd_v7_schema,
    _uses_welfare_center_schema,
)

# document
from .document import (
    _build_okms_document_name,
    _get_document_name,
    _get_document_snippet,
    _format_document_for_prompt,
    _format_facility_for_prompt,
)

# facility
from .facility import (
    _FACILITY_NAME_PATTERN,
    _FACILITY_KEYWORD_MAP,
    _load_facility_keyword_map,
    _get_facility_keyword_map,
    _extract_facility_type_from_message,
    _extract_specific_facility_name,
)

# extraction
from .extraction import (
    _LIFECYCLE_CONTENT_KEYWORDS,
    _LIFECYCLE_KEYWORD_MAP,
    _extract_sigun_from_message,
    _extract_years_from_message,
    _extract_birth_year_from_message,
    _extract_lifecycle_from_message,
    _birth_year_to_lifecycle,
    _filter_docs_by_lifecycle,
    _normalize_sigun_docs,
)

# query_builder
from .query_builder import (
    _build_search_queries,
    _build_comparison_search_queries,
    filter_okms_keywords,
)

# db_lookup
from .db_lookup import (
    _lookup_welfare_tel,
    _lookup_facility_from_db,
)

# welfare_search
from .welfare_search import (
    _LIFECYCLE_EXCLUDED_FACILITY_TYPES,
    _lookup_facility_by_name,
    _search_welfare_center_documents,
    _build_welfare_referenced_documents,
    _rerank_comparison_docs,
)

# token
from .token import (
    _TIKTOKEN_ENCODER,
    truncate_messages_by_token_limit,
    _truncate_text_by_tokens,
)

# pipeline_utils
from .pipeline_utils import (
    _build_documents_text,
    _build_qa_messages,
    _create_llm_params,
    _is_sufficient,
    _deduplicate_documents,
)

# 새 파이프라인 (services_v2에서 통합)
from .pipeline_general import process_rag_general
from .pipeline_comparison import process_rag_with_documents_v2
from .pipeline_guide_recommend import process_rag_guide_recommend
from .pipeline_search import process_rag_search
from .common import (
    sort_weight_top30_then_year,
    build_referenced_documents,
    filter_excluded_docs,
)
from .response_generator import generate_final_response_v2
