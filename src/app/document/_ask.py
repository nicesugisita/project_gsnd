"""업로드 문서 질의응답 핸들러"""

import asyncio
import json
import logging
import uuid
from typing import Any, Optional

from fastapi import Form
from fastapi.responses import JSONResponse

from app.core.config import Config

from app.core.constants import ROLE_USER, ROLE_ASSISTANT
from app.shared.schemas import ChatRequest
from app.chat.service import call_llm_api
from app.mariner.queryset_upload import query_mariner_documents
from app.conversation.history import get_chat_history_service
from app.document.uploaded import get_uploaded_document_service
from app.document.transfer import load_uploaded_text_content
from app.shared.utils import create_error_detail, load_uploaded_qa_prompt
from app.dependencies import _resolve_chat_history_user_id, _save_chat_history

logger = logging.getLogger(__name__)


async def ask_uploaded_document(
    question: Optional[str] = Form(None),
    conv_id: Optional[str] = Form(None),
    user_id: Optional[str] = Form(None),
    chat_history: Optional[str] = Form(None),
    max_documents: int = Form(5),
):
    if user_id is None or not str(user_id).strip():
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body", "user_id"],
                    msg="user_id가 필요합니다.",
                    type_="value_error",
                )]
            },
            status_code=400,
        )

    if not question or not question.strip():
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body", "question"],
                    msg="질문이 필요합니다.",
                    type_="value_error",
                )]
            },
            status_code=400,
        )

    uploaded_document_service = get_uploaded_document_service()
    documents = uploaded_document_service.get_uploaded_documents(
        conv_id=conv_id,
        user_id=user_id,
        limit=max_documents,
    )

    if not documents:
        return JSONResponse(
            content={
                "question": question,
                "answer": "현재 대화에서 업로드된 문서를 찾지 못했습니다.",
                "meta": {
                    "conv_id": conv_id,
                    "user_id": user_id,
                    "documents_found": 0,
                    "documents_loaded": 0,
                },
            },
            status_code=200,
        )

    loaded_chunks: list[str] = []
    loaded_document_count = 0
    max_chars_total = 30000
    current_chars = 0
    qa_source = "mariner"
    referenced_documents: list[dict[str, Any]] = []

    allowed_names = {
        str(document.get("name") or "").strip()
        for document in documents
        if str(document.get("name") or "").strip()
    }

    normalized_conv_id = str((documents[0].get("conv_id") if documents else conv_id) or conv_id or "").strip()
    normalized_user_id = str((documents[0].get("user_id") if documents else user_id) or user_id or "").strip()

    mariner_candidates: list[dict[str, Any]] = []
    try:
        mariner_results = await asyncio.to_thread(
            query_mariner_documents,
            question.strip(),
            Config.MARINER_UPLOAD_COLLECTION,
            max(max_documents * 5, 10),
            normalized_user_id,
            normalized_conv_id,
        )
        mariner_candidates = [
            item for item in (mariner_results or [])
            if str(item.get("NAME") or "").strip() in allowed_names
        ]
    except Exception as mariner_error:
        logger.warning("업로드 문서 질문 Mariner 검색 실패: %s", mariner_error)

    if mariner_candidates:
        for item in mariner_candidates:
            content = str(item.get("CHUNK_PATH") or "").strip()
            name = str(item.get("NAME") or "").strip()
            chunk_id = str(item.get("CHUNK_ID") or "").strip()

            # CHUNK_PATH가 파일 경로인 경우 실제 파일 내용을 읽음
            # Mariner가 경로에 공백을 포함해 반환하는 경우 제거 (예: "....  txt" → "....txt")
            if content.startswith("/"):
                content = content.replace(" ", "")
                load_result = await asyncio.to_thread(load_uploaded_text_content, content)
                if load_result.get("ok"):
                    content = load_result.get("content") or ""
                else:
                    logger.warning("Mariner CHUNK_PATH 파일 읽기 실패: path=%s, reason=%s", content, load_result.get("message"))
                    content = ""

            if not content:
                continue

            remain = max_chars_total - current_chars
            if remain <= 0:
                break

            clipped_content = content[:remain]
            loaded_chunks.append(f"[문서명] {name}\n[내용]\n{clipped_content}")
            current_chars += len(clipped_content)

            if name and chunk_id and not any(
                ref.get("name") == name and ref.get("chunk_id") == chunk_id
                for ref in referenced_documents
            ):
                referenced_documents.append({"name": name, "chunk_id": chunk_id})

        loaded_document_count = len(referenced_documents)
    else:
        qa_source = "file_fallback"
        for document in documents:
            path = str(document.get("path") or "")
            load_result = await asyncio.to_thread(load_uploaded_text_content, path)
            if not load_result.get("ok"):
                logger.warning(
                    "업로드 문서 텍스트 로드 실패: id=%s, path=%s, reason=%s",
                    document.get("id"),
                    path,
                    load_result.get("message"),
                )
                continue

            content = (load_result.get("content") or "").strip()
            if not content:
                continue

            remain = max_chars_total - current_chars
            if remain <= 0:
                break

            clipped_content = content[:remain]
            name = str(document.get("name") or "")
            loaded_chunks.append(f"[문서명] {name}\n[내용]\n{clipped_content}")
            loaded_document_count += 1
            current_chars += len(clipped_content)
            referenced_documents.append({"name": name, "chunk_id": str(document.get("id") or "")})

    if not loaded_chunks:
        return JSONResponse(
            content={
                "question": question,
                "answer": "업로드된 문서 본문을 불러오지 못했습니다.",
                "meta": {
                    "conv_id": conv_id,
                    "user_id": user_id,
                    "documents_found": len(documents),
                    "documents_loaded": 0,
                },
            },
            status_code=200,
        )

    context_text = "\n\n".join(loaded_chunks)
    history_for_llm: list[dict[str, str]] = []
    if chat_history:
        try:
            parsed_history = json.loads(chat_history)
            if isinstance(parsed_history, list):
                for item in parsed_history[-8:]:
                    if not isinstance(item, dict):
                        continue
                    role = str(item.get("role") or "").strip()
                    content = str(item.get("content") or "").strip()
                    if role in {ROLE_USER, ROLE_ASSISTANT} and content:
                        history_for_llm.append({"role": role, "content": content})
        except Exception as e:
            logger.warning("문서 질문 chat_history 파싱 실패: %s", e)

    qa_prompt_template = load_uploaded_qa_prompt()
    qa_system_prompt = qa_prompt_template

    try:
        answer = await call_llm_api(
            temperature=0,
            messages=[
                *history_for_llm,
                {
                    "role": "user",
                    "content": (
                        "[업로드 문서 컨텍스트]\n"
                        f"{context_text}\n\n"
                        f"질문: {question.strip()}"
                    ),
                },
            ],
            extra_system_prompts=[qa_system_prompt],
            max_tokens=1200,
        )
        answer_text = answer.strip() if isinstance(answer, str) else ""
        if not answer_text:
            answer_text = "문서 기반 답변을 생성하지 못했습니다."
    except Exception as e:
        logger.error(f"업로드 문서 질문 답변 실패: {e}", exc_info=True)
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body", "question"],
                    msg=f"문서 질문 처리 실패: {str(e)}",
                    type_="internal_error",
                )]
            },
            status_code=500,
        )

    # conv_id가 없으면 uuid로 생성
    if not conv_id:
        conv_id = str(uuid.uuid4())

    # DB에서 기존 전체 history 불러오기 (누적 저장)
    history_service = get_chat_history_service(Config)
    history_scope = _resolve_chat_history_user_id(user_id, conv_id)
    existing_history = (
        history_service.get_history(conv_id, history_scope) if conv_id else []
    )
    # 새 user/assistant 메시지 append
    existing_history.append({"role": "user", "content": question.strip()})
    existing_history.append({"role": ROLE_ASSISTANT, "content": answer_text})

    # ChatRequest 객체 생성
    chat_request = ChatRequest(
        user_id=user_id,
        conv_id=conv_id,
        messages=existing_history
    )

    _save_chat_history(
        chat_request,
        answer_text,
        user_message=question.strip(),
        referenced_documents=referenced_documents,
    )

    return JSONResponse(
        content={
            "question": question,
            "answer": answer_text,
            "meta": {
                "conv_id": conv_id,
                "user_id": user_id,
                "documents_found": len(documents),
                "documents_loaded": loaded_document_count,
                "context_chars": current_chars,
                "source": qa_source,
                "referenced_documents": referenced_documents,
                "history_used": len(history_for_llm),
            },
        },
        status_code=200,
    )
