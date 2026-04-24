"""
ASGI application entry point.

Provides the application instance for Uvicorn or other ASGI servers.
"""

import uvicorn

from app import app
from core.config import get_config


if __name__ == "__main__":
    config = get_config()
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        reload=config.DEBUG,
        log_level=config.LOG_LEVEL.lower()
    )
