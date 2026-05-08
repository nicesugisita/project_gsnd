"""LLM API 핵심 호출 로직"""

import json
import logging
import time
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, Optional

import httpx

from app.core.config import Config
from app.core.constants import (
    CHAT_COMPLETIONS_ENDPOINT,
    ROLE_SYSTEM, ROLE_USER,
    LLM_SYSTEM_MSG_MAX_CHARS, LLM_MSG_MAX_CHARS, LLM_TRUNCATE_HEAD_RATIO,
    LOG_LLM_REQUEST_TIME, LOG_LLM_RESPONSE_TIME,
    LOG_LLM_CLIENT_TIME, LOG_LLM_API_TIME,
    LOG_LLM_RESPONSE_COMPLETE, LOG_LLM_COMPLETE,
)
from app.core.exceptions import LLMServiceError, StreamingError
from app.shared.utils.prompt_loader import load_system_prompt

from .client import _get_llm_client, _reset_llm_client, _POOL_RETRY_ERRORS

logger = logging.getLogger(__name__)


def _build_messages(
    system_prompt: Optional[str] = None,
    extra_system_prompts: Optional[list] = None,
    messages: Optional[list] = None,
    message: Optional[str] = None
) -> list:
    """최종 메시지 목록 구성"""
    final_messages = []

    if extra_system_prompts is not None:
        for sys_prompt in extra_system_prompts:
            if sys_prompt:
                final_messages.append({"role": ROLE_SYSTEM, "content": sys_prompt})
    elif system_prompt:
        final_messages.append({"role": ROLE_SYSTEM, "content": system_prompt})
    else:
        final_messages.append({"role": ROLE_SYSTEM, "content": load_system_prompt()})

    if messages:
        for msg in messages:
            if msg.get("role") != ROLE_SYSTEM:
                content = msg.get("content", "")
                if not isinstance(content, str):
                    content = str(content)
                msg_copy = dict(msg)
                msg_copy["content"] = content
                final_messages.append(msg_copy)
        if message:
            last_msg = messages[-1] if messages else None
            if not last_msg or not (last_msg.get("role") == ROLE_USER and last_msg.get("content") == message):
                final_messages.append({"role": ROLE_USER, "content": message})
    elif message:
        final_messages.append({"role": ROLE_USER, "content": message})

    return final_messages


def _build_payload(
    final_messages: list,
    temperature: float,
    max_tokens: Optional[int],
    response_format: Optional[Dict[str, str]],
    frequency_penalty: float,
    repetition_penalty: float,
    top_p: int,
    top_k: int,
    seed: Optional[int],
    tools: Optional[list],
    stream: bool
) -> Dict[str, Any]:
    """LLM API 요청 페이로드 구성"""
    payload: Dict[str, Any] = {
        "messages": final_messages,
        "temperature": temperature,
        "frequency_penalty": frequency_penalty,
        "repetition_penalty": repetition_penalty,
        "top_p": top_p,
        "top_k": top_k,
        "stream": stream,
    }

    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if response_format:
        payload["response_format"] = response_format
    if seed is not None:
        payload["seed"] = seed
    if tools:
        payload["tools"] = tools

    return payload


def _truncate_for_context(text: Any, max_chars: int) -> Any:
    if not isinstance(text, str):
        return text
    if len(text) <= max_chars:
        return text
    head_len = int(max_chars * LLM_TRUNCATE_HEAD_RATIO)
    tail_len = max_chars - head_len - len("\n...[중략]...\n")
    if tail_len < 0:
        tail_len = 0
    return f"{text[:head_len]}\n...[중략]...\n{text[-tail_len:] if tail_len > 0 else ''}"


def _compact_payload_for_context_retry(payload: Dict[str, Any]) -> Dict[str, Any]:
    compact_payload = dict(payload)
    messages = list(compact_payload.get("messages", []))

    system_messages = [msg for msg in messages if msg.get("role") == ROLE_SYSTEM]
    non_system_messages = [msg for msg in messages if msg.get("role") != ROLE_SYSTEM]
    non_system_messages = non_system_messages[-6:]

    compact_messages = []
    for msg in system_messages[:2]:
        compact_messages.append({
            "role": msg.get("role"),
            "content": _truncate_for_context(msg.get("content", ""), LLM_SYSTEM_MSG_MAX_CHARS)
        })
    for msg in non_system_messages:
        compact_messages.append({
            "role": msg.get("role"),
            "content": _truncate_for_context(msg.get("content", ""), LLM_MSG_MAX_CHARS)
        })

    compact_payload["messages"] = compact_messages
    compact_payload["tools"] = []
    return compact_payload


def _is_context_length_error(response: httpx.Response) -> bool:
    if response.status_code != 400:
        return False
    body_text = response.text.lower()
    return (
        "maximum context length" in body_text
        or "input tokens" in body_text
        or "reduce the length of the input messages" in body_text
    )


async def _call_llm_api_sync(
    api_url: str,
    payload: Dict[str, Any],
    allow_context_retry: bool = True,
    timeout: Optional[int] = None,
    http_client: Optional[httpx.AsyncClient] = None,
) -> str:
    """동기 LLM API 호출. http_client가 주입되면 모듈 전역 대신 사용."""
    t0 = time.monotonic()
    request_time = datetime.now()

    try:
        # 마지막 user 메시지 추출해서 로깅
        _user_msgs = [m for m in payload.get("messages", []) if m.get("role") == ROLE_USER]
        _last_user_full = _user_msgs[-1].get("content", "") if _user_msgs else ""
        _last_user_preview = str(_last_user_full)[:120] if _last_user_full else "(없음)"
        logger.info("[LLM Request] user_content_len=%d, preview=%s", len(str(_last_user_full or "")), _last_user_preview)
        logger.debug(f"[LLM Request] Full payload: {json.dumps(payload, ensure_ascii=False, indent=2)}")

        client = http_client or _get_llm_client()
        t1 = time.monotonic()
        response = await client.post(
            api_url, json=payload,
            timeout=timeout if timeout is not None else Config.LLM_API_TIMEOUT,
        )
        t2 = time.monotonic()
        response_time = datetime.now()

        logger.info(LOG_LLM_REQUEST_TIME, request_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
        logger.info(LOG_LLM_RESPONSE_TIME, response_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
        logger.info(LOG_LLM_CLIENT_TIME, t1 - t0)
        logger.info(LOG_LLM_API_TIME, t2 - t1)

        if response.status_code == 200:
            result = response.json()
            t3 = time.monotonic()
            logger.info(LOG_LLM_RESPONSE_COMPLETE, t3 - t2)
            logger.debug(f"LLM API 응답: {json.dumps(result, ensure_ascii=False)}")

            if "choices" in result and len(result["choices"]) > 0:
                message = result["choices"][0].get("message") or {}
                content = message.get("content")
                if content is not None:
                    logger.info(LOG_LLM_COMPLETE, response.status_code, len(str(content)))
                    logger.info(f"[LLM Response] {str(content)[:500]}")
                    return content

        if allow_context_retry and _is_context_length_error(response):
            compact_payload = _compact_payload_for_context_retry(payload)
            logger.warning(
                "LLM 컨텍스트 초과 감지 - 축약 재시도 수행 (messages: %d -> %d)",
                len(payload.get("messages", [])),
                len(compact_payload.get("messages", []))
            )
            return await _call_llm_api_sync(api_url, compact_payload, allow_context_retry=False, timeout=timeout, http_client=http_client)

        error_detail = ""
        try:
            error_detail = response.text[:500]
            try:
                error_json = response.json()
                logger.error(f"[LLM Error Response] {json.dumps(error_json, ensure_ascii=False)}")
            except Exception:
                logger.error(f"[LLM Error Response] {error_detail}")
        except Exception:
            pass
        logger.error(f"LLM API 오류 (status: {response.status_code}) - {error_detail}")
        raise LLMServiceError(f"LLM 응답 오류 (status: {response.status_code})")

    except _POOL_RETRY_ERRORS as e:
        # 주입된 클라이언트면 재생성 건너뜀 (테스트/외부 주입 클라이언트는 직접 관리)
        if http_client is not None:
            raise LLMServiceError(f"LLM 호출 실패: {e}") from e
        logger.warning("LLM API 커넥션 풀 오류 → 클라이언트 재생성 후 1회 재시도: %s", e)
        await _reset_llm_client()
        client = _get_llm_client()
        response = await client.post(api_url, json=payload, timeout=timeout if timeout is not None else Config.LLM_API_TIMEOUT)
        if response.status_code == 200:
            result = response.json()
            if "choices" in result and len(result["choices"]) > 0:
                message = result["choices"][0].get("message") or {}
                content = message.get("content")
                if content is not None:
                    return content
        raise LLMServiceError(f"LLM 재시도 실패 (status: {response.status_code})") from e
    except httpx.ReadTimeout as e:
        logger.error("LLM API 타임아웃", exc_info=True)
        raise LLMServiceError("LLM 응답 시간 초과") from e
    except httpx.ConnectError as e:
        logger.error("LLM API 연결 실패", exc_info=True)
        raise LLMServiceError("LLM 서버 연결 실패") from e


async def _stream_llm_response(
    api_url: str,
    payload: Dict[str, Any]
) -> AsyncGenerator[str, None]:
    """스트리밍 LLM 응답 처리"""
    try:
        client = _get_llm_client()
        async with client.stream("POST", api_url, json=payload) as response:
            if response.status_code != 200:
                error_body = await response.aread()
                decoded_error = error_body.decode("utf-8", errors="replace")[:1000]
                logger.error(
                    "LLM API 스트리밍 오류 (status: %s) - body: %s",
                    response.status_code, decoded_error
                )

                fallback_payload = dict(payload)
                fallback_payload["stream"] = False
                try:
                    fallback_content = await _call_llm_api_sync(api_url, fallback_payload)
                    if fallback_content:
                        chunk_data = {
                            "choices": [{
                                "index": 0,
                                "delta": {"content": fallback_content},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                except Exception as fallback_error:
                    logger.error("LLM non-stream 폴백 실패: %s", fallback_error, exc_info=True)

                yield f"data: {{\"error\": \"LLM 응답 오류 (status: {response.status_code})\"}}\n\n"
                return

            async for line in response.aiter_lines():
                if line.strip():
                    if line.startswith("data: "):
                        yield line + "\n\n"
    except _POOL_RETRY_ERRORS as e:
        logger.warning("LLM API 스트리밍 커넥션 풀 오류 → 클라이언트 재생성 후 1회 재시도: %s", e)
        await _reset_llm_client()
        client = _get_llm_client()
        async with client.stream("POST", api_url, json=payload) as response:
            if response.status_code != 200:
                yield f"data: {{\"error\": \"LLM 재시도 실패 (status: {response.status_code})\"}}\n\n"
                return
            async for line in response.aiter_lines():
                if line.strip() and line.startswith("data: "):
                    yield line + "\n\n"
    except httpx.ReadTimeout as e:
        logger.error("LLM API 스트리밍 타임아웃", exc_info=True)
        raise StreamingError("스트리밍 응답 시간 초과") from e
    except httpx.ConnectError as e:
        logger.error("LLM API 스트리밍 연결 실패", exc_info=True)
        raise StreamingError("스트리밍 서버 연결 실패") from e
    except Exception as e:
        logger.error(f"LLM API 스트리밍 오류: {e}", exc_info=True)
        raise StreamingError(f"스트리밍 처리 중 오류: {str(e)}") from e


async def call_llm_api(
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
    seed: Optional[int] = Config.FIXED_LLM_SEED,
    tools: Optional[list] = None,
    stream: bool = False,
    api_url: Optional[str] = None,
    timeout: Optional[int] = None,
    http_client: Optional[httpx.AsyncClient] = None,
) -> str | AsyncGenerator[str, None]:
    """
    LLM API 호출 (동기/비동기 모두 지원)

    Args:
        message: 사용자 메시지 (단일 턴)
        temperature: 응답의 창의성 (0~2)
        max_tokens: 최대 생성 토큰 수
        system_prompt: 시스템 프롬프트
        response_format: 응답 형식
        messages: 멀티턴 메시지 히스토리
        extra_system_prompts: 추가 시스템 프롬프트 목록
        stream: 스트리밍 응답 여부
        api_url: 커스텀 API URL

    Returns:
        LLM 응답 (stream=False) 또는 비동기 제너레이터 (stream=True)

    Raises:
        LLMServiceError: LLM API 호출 실패 시
    """
    if not Config.LLM_ENABLED or not Config.LLM_API_URL:
        raise LLMServiceError("LLM이 비활성화되어 있습니다.")

    try:
        base_url = api_url or Config.LLM_API_URL
        api_endpoint = f"{base_url}{CHAT_COMPLETIONS_ENDPOINT}"

        build_messages_kwargs = {
            "system_prompt": system_prompt,
            "extra_system_prompts": extra_system_prompts
        }
        if messages and message:
            last_msg = messages[-1] if messages else None
            if last_msg and last_msg.get("role") == ROLE_USER and last_msg.get("content") == message:
                build_messages_kwargs["messages"] = messages
            else:
                build_messages_kwargs["message"] = message
        elif messages:
            build_messages_kwargs["messages"] = messages
        elif message:
            build_messages_kwargs["message"] = message

        final_messages = _build_messages(**build_messages_kwargs)

        payload = _build_payload(
            final_messages=final_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            frequency_penalty=frequency_penalty,
            repetition_penalty=repetition_penalty,
            top_p=top_p,
            top_k=top_k,
            seed=seed,
            tools=tools,
            stream=stream
        )

        if stream:
            return _stream_llm_response(api_endpoint, payload)
        else:
            return await _call_llm_api_sync(api_endpoint, payload, timeout=timeout, http_client=http_client)

    except LLMServiceError:
        raise
    except Exception as e:
        logger.error(f"LLM API 호출 오류: {e}", exc_info=True)
        raise LLMServiceError(f"LLM 처리 중 오류: {str(e)}") from e
