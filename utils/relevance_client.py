import logging
import time
from typing import Dict, Optional
import httpx

from core.config import Config
from core.constants import CHAT_COMPLETIONS_ENDPOINT

logger = logging.getLogger(__name__)
# ============================================================
# 전용 LLM API 클라이언트
# ============================================================

async def _call_relevance_llm(
    message: str = None,
    temperature: float = 0,
    max_tokens: Optional[int] = 8192,
    system_prompt: Optional[str] = None,
    response_format: Optional[Dict[str, str]] = None,
    messages: Optional[list] = None,
    extra_system_prompts: Optional[list] = None,
    frequency_penalty: float = 0,
    repetition_penalty: float = 1.0,
    top_p: int = 1,
    top_k: int = 1,
    seed: Optional[int] = None,
    tools: Optional[list] = None,
    stream: bool = False,
    api_url: Optional[str] = None,
    timeout: Optional[int] = None,
) -> str:
    """
    관련성 필터 전용 LLM API 클라이언트

    RELEVANCE_LLM_API_URL을 기본 엔드포인트로 사용합니다.
    """
    base_url = api_url or Config.RELEVANCE_LLM_API_URL
    api_endpoint = f"{base_url}{CHAT_COMPLETIONS_ENDPOINT}"

    # 메시지 구성
    final_messages = []
    if extra_system_prompts is not None:
        for sp in extra_system_prompts:
            if sp:
                final_messages.append({"role": "system", "content": sp})
    elif system_prompt:
        final_messages.append({"role": "system", "content": system_prompt})

    if messages:
        for msg in messages:
            if msg.get("role") != "system":
                final_messages.append(msg)
    if message:
        final_messages.append({"role": "user", "content": message})

    # 페이로드 구성
    payload = {
        "model": Config.RELEVANCE_LLM_MODEL_NAME,
        "messages": final_messages,
        "temperature": temperature,
        "max_completion_tokens": max_tokens or 8192,
        "frequency_penalty": frequency_penalty,
        "repetition_penalty": repetition_penalty,
        "top_p": top_p,
        "top_k": top_k,
        "stream": stream,
        "seed": seed,
    }
    if tools:
        payload["tools"] = tools
    if response_format:
        payload["response_format"] = response_format

    # API 호출
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout or Config.LLM_API_TIMEOUT) as client:
            response = await client.post(api_endpoint, json=payload)
        elapsed = time.monotonic() - t0
        logger.debug(f"[RelevanceFilter/LLM] {api_endpoint} → {response.status_code} ({elapsed:.3f}s)")

        if response.status_code == 200:
            result = response.json()
            if "choices" in result and len(result["choices"]) > 0:
                msg = result["choices"][0].get("message", {})
                # content 우선, reasoning_content 폴백
                content = msg.get("content")
                if content is not None:
                    return content
                reasoning = msg.get("reasoning_content")
                if reasoning is not None:
                    return reasoning

        logger.warning(f"[RelevanceFilter/LLM] 응답 오류: status={response.status_code}, body={response.text[:500]}")
        raise RuntimeError(f"Relevance LLM API 오류: {response.status_code}")

    except httpx.TimeoutException as e:
        logger.warning(f"[RelevanceFilter/LLM] 타임아웃: {e}")
        raise
    except Exception as e:
        if not isinstance(e, RuntimeError):
            logger.warning(f"[RelevanceFilter/LLM] 호출 실패: {e}")
        raise