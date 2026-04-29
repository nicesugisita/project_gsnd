"""앱 패키지 — 라우터 집약 등 진입 헬퍼."""

from fastapi import FastAPI


def register_routes(app: FastAPI) -> None:
    """모든 도메인 라우터를 FastAPI 앱에 등록한다."""
    from app.chat.router import router as chat_router
    from app.conversation.router import router as conversation_router
    from app.document.router import router as document_router
    from app.keyword.router import router as keyword_router
    from app.rag.router import router as rag_router
    from app.system.router import router as system_router
    from app.tts.router import router as tts_router

    app.include_router(chat_router)
    app.include_router(rag_router)
    app.include_router(conversation_router)
    app.include_router(document_router)
    app.include_router(tts_router)
    app.include_router(keyword_router)
    app.include_router(system_router)


__all__ = ["register_routes"]
