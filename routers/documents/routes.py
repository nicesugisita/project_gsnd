"""Documents 라우터 — 라우트 등록"""

from fastapi import APIRouter
from .upload import upload_file
from .download import download_document, extract_document_text, extract_and_summarize_document
from .ask import ask_uploaded_document

router = APIRouter()

router.post("/v1/files/upload")(upload_file)
router.get("/v1/documents/download")(download_document)
router.post("/v1/documents/extract-text")(extract_document_text)
router.post("/v1/documents/extract-and-summarize")(extract_and_summarize_document)
router.post("/v1/documents/ask")(ask_uploaded_document)
