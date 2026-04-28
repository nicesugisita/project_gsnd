"""문서 다운로드 및 텍스트 추출 핸들러"""

import asyncio
import logging
import os
import tempfile
from typing import Any, Dict, Optional

from fastapi import UploadFile, File
from fastapi.responses import JSONResponse, FileResponse

from app.core.config import Config

from app.core.constants import ROLE_ASSISTANT
from app.mariner.parsing import parse_file
from app.shared.schemas import ExtractAndSummarizeRequest
from app.chat.service import summarize_document_text
from app.document.service_impl import get_document_service
from app.chat.mariner_index import trigger_mariner_index
from app.document.uploaded import get_uploaded_document_service
from app.document.transfer import transfer_uploaded_text_to_remote
from app.shared.utils import create_error_detail

logger = logging.getLogger(__name__)


async def download_document(doc_id: Optional[str] = None, doc_name: Optional[str] = None, path: Optional[str] = None):
    """
    Download a document by ID and name.

    Args:
        doc_id: Document ID from the dataset
        doc_name: Document name from the dataset
        path: Direct file path from Mariner search result (fallback when DB lookup fails)

    Returns:
        File response with the document
    """
    if not doc_id and not doc_name and not path:
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["query"],
                    msg="doc_id or doc_name is required",
                    type_="value_error",
                )]
            },
            status_code=400,
        )

    try:
        doc_service = get_document_service(Config)
        doc_info = None
        if doc_name:
            doc_info = doc_service.get_document_by_name(doc_name)
        elif doc_id:
            doc_info = doc_service.get_document_by_id(doc_id)

        # GSND 조회 실패 시 OKMS(okms2.VIEW_WLF_SRVC)에서 ORG_NM으로 fallback
        if not doc_info and doc_name:
            doc_info = doc_service.get_okms_document_by_name(doc_name)

        # DB 조회 실패 시 Mariner 결과의 직접 경로(path)로 fallback
        if not doc_info and path:
            is_valid, error_msg = doc_service.validate_file_path(path)
            if is_valid:
                resolved_path = doc_service.resolve_file_path(path)
                filename = doc_name or os.path.basename(path)
                logger.info(f"Downloading document via direct path: {filename} from {resolved_path}")
                return FileResponse(
                    path=resolved_path,
                    filename=filename,
                    media_type='application/octet-stream'
                )
            else:
                logger.warning(f"Direct path fallback invalid: {path} — {error_msg}")

        if not doc_info:
            return JSONResponse(
                content={
                    "detail": [create_error_detail(
                        loc=["query"],
                        msg="Document not found",
                        type_="not_found",
                    )]
                },
                status_code=404,
            )

        # Verify that the ID matches when both are provided
        if doc_id and doc_name and doc_info.get('id') and str(doc_info['id']) != str(doc_id):
            logger.warning(
                f"Document ID mismatch: requested {doc_id} for name {doc_name}, "
                f"found {doc_info['id']}"
            )

        file_path = doc_info['path']
        resolved_path = doc_service.resolve_file_path(file_path)

        # Validate file path
        is_valid, error_msg = doc_service.validate_file_path(file_path)
        if not is_valid:
            logger.error(f"Invalid file path: {file_path}")
            return JSONResponse(
                content={
                    "detail": [create_error_detail(
                        loc=["query"],
                        msg=error_msg,
                        type_="not_found",
                    )]
                },
                status_code=404,
            )

        # Get filename for download - use original document name
        filename = doc_info['name']

        logger.info(f"Downloading document: {doc_name} from {resolved_path}")

        return FileResponse(
            path=resolved_path,
            filename=filename,
            media_type='application/octet-stream'
        )

    except Exception as e:
        logger.error(f"Error downloading document: {e}")
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["query"],
                    msg="Failed to download document",
                    type_="internal_error",
                )]
            },
            status_code=500,
        )


async def extract_document_text(file: Optional[UploadFile] = File(None)):
    """
    Extract text from uploaded file.

    Args:
        file: Uploaded file

    Returns:
        JSON response with extracted text
    """
    if file is None:
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body", "file"],
                    msg="업로드할 파일이 필요합니다.",
                    type_="value_error",
                )]
            },
            status_code=400,
        )

    if not file.filename:
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body", "file"],
                    msg="파일명이 없습니다",
                    type_="value_error",
                )]
            },
            status_code=400,
        )

    ext = os.path.splitext(file.filename)[1].lower()

    # Fix: collection_name was undefined in the original code; initialise to None
    collection_name = None
    temp_file_path: Optional[str] = None
    index_meta: Dict[str, Any] = {
        "requested": False,
        "ok": False,
        "collection_name": collection_name or Config.MARINER_UPLOAD_COLLECTION,
        "message": "not_requested",
    }

    try:
        file_content = await file.read()
        if not file_content:
            return JSONResponse(
                content={
                    "detail": [create_error_detail(
                        loc=["body", "file"],
                        msg="빈 파일은 처리할 수 없습니다",
                        type_="value_error",
                    )]
                },
                status_code=400,
            )

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
            temp_file.write(file_content)
            temp_file_path = temp_file.name

        extracted_text = await asyncio.to_thread(parse_file, temp_file_path)

        return JSONResponse(
            content={
                "filename": file.filename,
                "text": extracted_text,
            },
            status_code=200,
        )
    except Exception as e:
        logger.error(f"문서 텍스트 추출 실패: {e}", exc_info=True)
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body", "file"],
                    msg=f"문서 파싱 실패: {str(e)}",
                    type_="internal_error",
                )]
            },
            status_code=500,
        )
    finally:
        await file.close()
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception as cleanup_error:
                logger.warning(f"임시 파일 삭제 실패: {cleanup_error}")


async def extract_and_summarize_document(
    body: ExtractAndSummarizeRequest
):
    """
    Extract text from document at given path and summarize it.

    Args:
        file_path: 서버 상의 파일 경로

    Returns:
        JSON response with extracted text and summary
    """
    import uuid
    from datetime import datetime

    file_path = body.file_path.strip()
    collection_name = body.collection_name
    conv_id = body.conv_id
    user_id = body.user_id
    filename = os.path.basename(file_path)

    if not os.path.exists(file_path):
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body", "file_path"],
                    msg=f"파일을 찾을 수 없습니다: {file_path}",
                    type_="value_error",
                )]
            },
            status_code=400,
        )

    index_meta = {
        "requested": False,
        "ok": False,
        "collection_name": collection_name or Config.MARINER_UPLOAD_COLLECTION,
        "message": "not_requested",
    }

    try:
        extracted_text = await asyncio.to_thread(parse_file, file_path)

        text_store_dir = Config.UPLOADED_TEXT_REMOTE_DIR
        os.makedirs(text_store_dir, exist_ok=True)

        text_file_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}.txt"
        extracted_text_path = os.path.join(text_store_dir, text_file_name)

        with open(extracted_text_path, "w", encoding="utf-8") as output_file:
            output_file.write(extracted_text or "")

        stored_text_path = extracted_text_path
        transfer_result = await asyncio.to_thread(
            transfer_uploaded_text_to_remote,
            extracted_text_path,
        )
        if transfer_result.get("ok") and transfer_result.get("remote_path"):
            stored_text_path = transfer_result["remote_path"]
            try:
                os.remove(extracted_text_path)
            except Exception as cleanup_error:
                logger.warning("로컬 텍스트 파일 삭제 실패: %s", cleanup_error)
        elif transfer_result.get("message") != "remote transfer disabled":
            logger.warning(
                "업로드 텍스트 원격 전송 미완료, 로컬 경로 사용: %s",
                transfer_result.get("message"),
            )

        uploaded_document_service = get_uploaded_document_service()
        save_ok = uploaded_document_service.save_uploaded_document(
            conv_id=conv_id,
            user_id=user_id,
            name=filename,
            path=stored_text_path,
        )
        if not save_ok:
            logger.warning(
                "업로드 문서 메타데이터 저장 실패: conv_id=%s, user_id=%s, name=%s",
                conv_id,
                user_id,
                filename,
            )

        summary = await summarize_document_text(extracted_text)

        # 요약 결과를 대화내역(DB)에 저장
        try:
            from app.conversation.history import get_chat_history_service
            chat_history_service = get_chat_history_service()
            messages = [
                {"role": "user", "content": f"문서 요약 요청: {filename}"},
                {"role": ROLE_ASSISTANT, "content": summary}
            ]
            chat_history_service.upsert_history(user_id=user_id, conv_id=conv_id, messages=messages)
        except Exception as e:
            logger.error(f"문서 요약 대화내역 저장 실패: {e}")
            index_meta = {
                "collection_name": collection_name or Config.MARINER_UPLOAD_COLLECTION,
                "message": "db_save_failed",
            }

        ##############################################
        # Mariner 색인 요청
        index_res = await trigger_mariner_index(
            collection_name=collection_name or Config.MARINER_UPLOAD_COLLECTION,
        )
        index_meta = {
            "requested": True,
            "ok": index_res.get("ok", False),
            "collection_name": index_res.get("collection_name"),
            "message": index_res.get("message", "requested"),
        }

        # 색인 완료 대기 (색인 트리거 후 Mariner가 실제 색인을 완료할 때까지 대기)
        if index_res.get("ok"):
            await asyncio.sleep(10)
        ##############################################

        return JSONResponse(
            content={
                "filename": filename,
                "text": extracted_text,
                "summary": summary,
                "meta": {
                    "text_length": len(extracted_text or ""),
                    "summary_length": len(summary or ""),
                    "index": index_meta,
                }
            },
            status_code=200,
        )
    except Exception as e:
        logger.error(f"문서 텍스트 추출/요약 실패: {e}", exc_info=True)
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body", "file_path"],
                    msg=f"문서 파싱 또는 요약 실패: {str(e)}",
                    type_="internal_error",
                )]
            },
            status_code=500,
        )
