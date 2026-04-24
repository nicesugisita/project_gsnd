from fastapi import FastAPI
from .chat import router as chat_router
from .conversations import router as conversations_router
from .documents import router as documents_router
from .tts import router as tts_router
from .system import router as system_router
from .query import router as query_router
from .keywords import router as keywords_router


def register_routes(app: FastAPI) -> None:
    app.include_router(chat_router)
    app.include_router(conversations_router)
    app.include_router(documents_router)
    app.include_router(tts_router)
    app.include_router(system_router)
    app.include_router(query_router)
    app.include_router(keywords_router)
