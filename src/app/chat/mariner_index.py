import logging
from typing import Any, Dict, Optional

import httpx

from app.core.config import Config

logger = logging.getLogger(__name__)


def _resolve_collection_name(collection_name: Optional[str]) -> str:
    candidate = (collection_name or "").strip()
    if candidate:
        return candidate
    return Config.MARINER_UPLOAD_COLLECTION


async def trigger_mariner_index(collection_name: Optional[str] = None) -> Dict[str, Any]:
    api_url = (Config.MARINER_INDEX_API_URL or "").strip()
    resolved_collection_name = _resolve_collection_name(collection_name)

    if not api_url:
        logger.warning("MARINER_INDEX_API_URL 미설정으로 색인 요청을 건너뜁니다.")
        return {
            "ok": False,
            "status_code": None,
            "collection_name": resolved_collection_name,
            "message": "MARINER_INDEX_API_URL is empty",
        }

    params = {
        "collection": resolved_collection_name,
        "type": "full",
    }

    try:
        timeout = max(1, int(Config.MARINER_INDEX_TIMEOUT))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(api_url, params=params)

        is_ok = response.status_code < 400
        if is_ok:
            logger.info(
                "Mariner 색인 요청 성공: url=%s, collection=%s, status=%s",
                api_url,
                resolved_collection_name,
                response.status_code,
            )
        else:
            logger.warning(
                "Mariner 색인 요청 실패: url=%s, collection=%s, status=%s, body=%s",
                api_url,
                resolved_collection_name,
                response.status_code,
                (response.text or "")[:500],
            )

        return {
            "ok": is_ok,
            "status_code": response.status_code,
            "collection_name": resolved_collection_name,
            "message": (response.text or "")[:500],
        }
    except Exception as exc:
        logger.error(
            "Mariner 색인 요청 오류: collection=%s, error=%s",
            resolved_collection_name,
            exc,
            exc_info=True,
        )
        return {
            "ok": False,
            "status_code": None,
            "collection_name": resolved_collection_name,
            "message": str(exc),
        }
