"""RAG 서비스 패키지"""

# collection
from .collection import (
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
    _format_our_region_tel_for_prompt,
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
