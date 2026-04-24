"""
FastAPI application initialization and configuration.

Creates and configures the FastAPI application with logging, CORS, and routes.
"""

import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import Config, get_config
from routers import register_routes
from core.constants import (
    LOG_FORMAT,
    LOG_MAX_BYTES,
    LOG_BACKUP_COUNT,
    LOG_ENCODING,
)
from core.logging_context import RequestContextFilter

def _setup_logging(config: Config) -> None:
    """
    Configure application logging.

    Args:
        config: Application configuration object
    """
    log_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "logs"
    )
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "app.log")

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format=LOG_FORMAT,
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                log_file,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding=LOG_ENCODING
            )
        ],
        force=True,
    )
    context_filter = RequestContextFilter()
    root_logger = logging.getLogger()
    root_logger.addFilter(context_filter)
    for handler in root_logger.handlers:
        handler.addFilter(context_filter)

    # httpx/httpcore 라이브러리 로그 억제 (DEBUG 시에도 커넥션 로그 숨김)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logger = logging.getLogger(__name__)

    # JVM 싱글톤 초기화 (서버 기동 시 1회)
    from mariner_v2.jvm_manager import init_jvm
    try:
        init_jvm()
    except Exception as e:
        logger.error(f"JVM 초기화 실패: {e}", exc_info=True)

    # 프롬프트 전체 프리로드 (lru_cache 워밍업)
    from utils.prompt_loader import (
        load_system_prompt, load_query_reform_prompt, load_query_expansion_prompt,
        load_triple_extraction_prompt, load_convert_korean_prompt, load_ask_judgment_prompt,
        load_re_ask_prompt, load_rag_norag_judgment_prompt, load_retrieval_sufficiency_judgment_prompt,
        load_text_cleaning_prompt, load_voice_cleaning_prompt, load_final_response_prompt,
        load_general_or_care_prompt, load_query_recreation_prompt, load_suggest_questions_prompt,
        load_voice_print_before_prompt, load_intent_classification_prompt,
        load_classification_general_prompt, load_classification_comparison_prompt,
        load_comparison_extract_prompt, load_comparison_attribute_prompt,
        load_comparison_triple_prompt, load_classification_recommended_prompt,
        load_classification_search_prompt, load_region_age_collect_recommended_prompt,
        load_document_summary_prompt, load_uploaded_qa_prompt,
    )
    _prompt_loaders = [
        load_system_prompt, load_query_reform_prompt, load_query_expansion_prompt,
        load_triple_extraction_prompt, load_convert_korean_prompt, load_ask_judgment_prompt,
        load_re_ask_prompt, load_rag_norag_judgment_prompt, load_retrieval_sufficiency_judgment_prompt,
        load_text_cleaning_prompt, load_voice_cleaning_prompt, load_final_response_prompt,
        load_general_or_care_prompt, load_query_recreation_prompt, load_suggest_questions_prompt,
        load_voice_print_before_prompt, load_intent_classification_prompt,
        load_classification_general_prompt, load_classification_comparison_prompt,
        load_comparison_extract_prompt, load_comparison_attribute_prompt,
        load_comparison_triple_prompt, load_classification_recommended_prompt,
        load_classification_search_prompt, load_region_age_collect_recommended_prompt,
        load_document_summary_prompt, load_uploaded_qa_prompt,
    ]
    for loader in _prompt_loaders:
        loader()
    logger.info(f"[Startup] 프롬프트 {len(_prompt_loaders)}개 프리로드 완료")

    yield


def create_app(config: Config = None) -> FastAPI:
    """
    FastAPI application factory.

    Args:
        config: Application configuration object. If None, uses get_config()

    Returns:
        Configured FastAPI application instance
    """
    if config is None:
        config = get_config()

    _setup_logging(config)

    app = FastAPI(
        title=config.APP_NAME,
        description=config.APP_DESCRIPTION,
        version=config.APP_VERSION,
        lifespan=_lifespan,
    )

    # Configure CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routes
    register_routes(app)

    return app


# Create application instance
app = create_app()
