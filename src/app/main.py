"""FastAPI 애플리케이션 — 도메인 라우터 등록."""

import logging
import os
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Config, get_config
from app.core.lifespan import lifespan
from app.core.constants import LOG_FORMAT, LOG_MAX_BYTES, LOG_BACKUP_COUNT, LOG_ENCODING
from app.core.logging_context import RequestContextFilter


def _setup_logging(config: Config) -> None:
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "app.log")
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format=LOG_FORMAT,
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                log_file, maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT, encoding=LOG_ENCODING,
            ),
        ],
        force=True,
    )
    ctx = RequestContextFilter()
    root = logging.getLogger()
    root.addFilter(ctx)
    for h in root.handlers:
        h.addFilter(ctx)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def create_app(config: Config = None) -> FastAPI:
    if config is None:
        config = get_config()
    _setup_logging(config)

    application = FastAPI(
        title=config.APP_NAME,
        description=config.APP_DESCRIPTION,
        version=config.APP_VERSION,
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── 도메인 라우터 등록 ────────────────────────────────────────────────
    from app.chat.router import router as chat_router
    from app.rag.router import router as rag_router
    from app.conversation.router import router as conversation_router
    from app.document.router import router as document_router
    from app.tts.router import router as tts_router
    from app.keyword.router import router as keyword_router
    from app.system.router import router as system_router

    application.include_router(chat_router)
    application.include_router(rag_router)
    application.include_router(conversation_router)
    application.include_router(document_router)
    application.include_router(tts_router)
    application.include_router(keyword_router)
    application.include_router(system_router)

    return application


app = create_app()
