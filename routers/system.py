"""
Health and info endpoints.
"""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.config import Config

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get('/health')
async def health_check():
    """
    Health check endpoint for service monitoring.

    Returns:
        JSON response with service status
    """
    return JSONResponse(
        content={
            "status": "healthy",
            "service": "GSND-RAG-Chatbot",
            "version": Config.APP_VERSION,
        },
        status_code=200,
    )


@router.get('/info')
async def app_info():
    """
    Application information endpoint.

    Returns:
        JSON response with application metadata
    """
    return JSONResponse(
        content={
            "name": Config.APP_NAME,
            "version": Config.APP_VERSION,
            "description": Config.APP_DESCRIPTION,
        },
        status_code=200,
    )
