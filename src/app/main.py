"""FastAPI 애플리케이션 — 도메인 라우터 등록."""

import logging
import os
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Config, get_config
from app import register_routes
from app.core.lifespan import lifespan
from app.core.constants import (
    LOG_FORMAT,
    LOG_MAX_BYTES,
    LOG_BACKUP_COUNT,
    LOG_ENCODING,
)
from app.core.logging_context import RequestContextFilter


def _setup_logging(config: Config) -> None:
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    # Windows에서 다중 프로세스/다중 인스턴스가 동일 파일로 rollover하면 WinError 32가 날 수 있어
    # 프로세스별 로그 파일로 분리한다.
    pid = os.getpid()
    log_file = os.path.join(log_dir, f"app.{pid}.log")
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format=LOG_FORMAT,
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                log_file,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding=LOG_ENCODING,
                delay=True,
            ),
        ],
        force=True,
    )
    context_filter = RequestContextFilter()
    root_logger = logging.getLogger()
    root_logger.addFilter(context_filter)
    for handler in root_logger.handlers:
        handler.addFilter(context_filter)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def create_app(config: Config = None) -> FastAPI:
    if config is None:
        config = get_config()

    _setup_logging(config)

    app = FastAPI(
        title=config.APP_NAME,
        description=config.APP_DESCRIPTION,
        version=config.APP_VERSION,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_routes(app)

    return app


app = create_app()
