"""파일 업로드 핸들러"""

import logging
import os
import uuid
from datetime import datetime

from fastapi import UploadFile, File, Form
from fastapi.responses import JSONResponse

from app.core.config import Config

logger = logging.getLogger(__name__)


async def upload_file(
    file: UploadFile = File(...),
    user_id: str = Form(None),
    conv_id: str = Form(None)
):
    """
    파일 업로드 엔드포인트. UI에서 FormData로 파일 전송 시 사용.
    파일명 충돌 방지,  업로드 후 서버 폴더 저장.
    """
    try:
        os.makedirs(Config.UPLOAD_DIR, exist_ok=True)

        ext = os.path.splitext(file.filename)[1].lower()
        contents = await file.read()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{timestamp}_{file.filename}"
        file_path = os.path.join(Config.UPLOAD_DIR, filename)

        # conv_id가 없이 요청 받을 경우(첫 대화일경우), 자동 생성
        if not conv_id:
            conv_id = str(uuid.uuid4())

        with open(file_path, "wb") as buffer:
            buffer.write(contents)

        return JSONResponse({
            "success": True,
            "filename": filename,
            "path": file_path,
            "user_id": user_id,
            "conv_id": conv_id
        })
    except Exception as e:
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=500
        )
