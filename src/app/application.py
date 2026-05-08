"""
FastAPI application initialization and configuration.

Creates and configures the FastAPI application with logging, CORS, and routes.
"""

import logging
import os
import pathlib
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
    """
    Configure application logging.

    Args:
        config: Application configuration object
    """
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)

    # Windows에서는 여러 uvicorn 프로세스(또는 여러 인스턴스)가 동일 파일로 Rotating 시
    # rename(app.log -> app.log.1)이 WinError 32로 실패할 수 있다.
    # 프로세스별 로그 파일로 분리하여 파일 잠금/rollover 충돌을 방지한다.
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
        lifespan=lifespan,
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
