"""채팅 응답 헬퍼 함수 (clarify, no-rag, rag 모드 처리)"""

import asyncio
import json
from functools import partial
import logging

from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import Config

from app.core.constants import ROLE_USER, ROLE_ASSISTANT
from app.shared.schemas import ChatRequest
from app.chat.service import (
    call_llm_api,
    query_recreation,
    reform_query_if_needed,
)
from app.chat.sigun import (
    check_sigun,
    count_sigun_ask_attempts,
    MSG_SIGUN_FAILURE,
    MAX_SIGUN_ASK_ATTEMPTS,
)
from app.chat.intent_registry import RECOMMENDED_QUESTION_SPEC, lookup as _lookup_intent_spec
from app.shared.utils import (
    build_chat_response,
    load_system_prompt,
    shorten_text,
    get_last_assistant_message,
    get_second_last_user_message,
)
from app.dependencies import (
    _build_llm_kwargs,
    _update_user_message,
    _save_chat_history,
)
from ._doc_filter import (
    _enrich_referenced_documents,
    _filter_referenced_documents_by_response,
)
from ._stream_utils import (
    _build_streaming_response,
    _stream_delta_content,
    SSE_RESPONSE_HEADERS,
    drain_status_until_done,
)

logger = logging.getLogger(__name__)


def _build_assistant_preprocess_payload(
    *,
    user_message: str,
    intent: str,
    reformed_query: str | None,
    expanded_queries: list | None,
    search_target: str | None,
    policy_priority_tag: str | None,
    more_info: bool = False,
) -> dict:
    """히스토리 저장용 assistant preprocess 메타를 구성."""
    final_reformed = (reformed_query or user_message or "").strip()
    return {
        "query": user_message,
        "intent": intent or "general",
        "reformed_query": final_reformed,
        "expanded_queries": expanded_queries or ([final_reformed] if final_reformed else []),
        "more_info": bool(more_info),
        "search_target": search_target,
        "policy_priority_tag": policy_priority_tag,
    }


def _get_rag_processor(intent: str, *, recommended_question_route: bool = False):
    """일반 completions는 intent로, /v1/chat/recommended-question 은 recommended_question_route 로만 분기한다.

    intent → processor 매핑은 `chat.intent_registry` 의 IntentSpec 테이블이 담당한다.
    """
    if recommended_question_route:
        return RECOMMENDED_QUESTION_SPEC.processor
    return _lookup_intent_spec(intent).processor


async def _handle_clarify_response(
    user_message: str,
    clarify_question: str,
    chat_request: ChatRequest,
    stream: bool
) -> JSONResponse | StreamingResponse:
    """Handle clarification/re-ask response."""
    clarify_response = build_chat_response(
        response_message=clarify_question,
        user_message=user_message,
        model_name=Config.MODEL_NAME,
        conv_id=chat_request.conv_id,
        is_clarification=True,
    )

    if stream:
        return StreamingResponse(
            _build_streaming_response(
                clarify_response["choices"][0]["message"]["content"],
                clarify_response
            ),
            media_type="text/event-stream",
            headers=SSE_RESPONSE_HEADERS,
        )

    return JSONResponse(content=clarify_response, status_code=200)


async def _run_query_recreation(
    user_message: str,
    chat_request: "ChatRequest",
    is_clarification: bool,
) -> tuple[str, bool]:
    """
    되묻기 응답인 경우 원질문 + 되묻기 + 답변을 조합해 완성 질의를 생성합니다.

    Returns:
        (user_message, skip_clarification_check).
        skip_clarification_check는 과거 통합 전처리에 메시지를 생략할 때 썼으며, 현재는 호환용으로만 유지한다.
    """
    # 짧은 후속 질문 감지: 20자 이하이고 이전 대화가 4턴 이상이면 맥락 재구성
    _prior_turns = [m for m in chat_request.messages[:-1] if m.get("role") in (ROLE_USER, ROLE_ASSISTANT)]
    _is_short_followup = (
        not is_clarification
        and len(user_message.strip()) <= 20
        and len(_prior_turns) >= 4
    )

    if not is_clarification and not _is_short_followup:
        return user_message, False

    last_clarify_question = get_last_assistant_message(chat_request.messages[:-1])
    original_user_question = get_second_last_user_message(chat_request.messages)

    if not (original_user_question and last_clarify_question):
        return user_message, False

    if is_clarification:
        logger.info(
            "[Query Recreation] 되묻기 답변 감지 | 원질문: %s | 되묻기: %s | 답변: %s",
            shorten_text(original_user_question, 50),
            shorten_text(last_clarify_question, 50),
            shorten_text(user_message, 50),
        )
    else:
        logger.info(
            "[Query Recreation] 짧은 후속 질문 감지(%d자) | 이전 답변: %s | 현재: %s",
            len(user_message.strip()),
            shorten_text(last_clarify_question, 50),
            shorten_text(user_message, 50),
        )

    recreated_query = await query_recreation(
        initial_query=original_user_question,
        messages=chat_request.messages,
    )
    if recreated_query:
        logger.info("[Query Recreation] 완성 질의 생성 성공: %s", shorten_text(recreated_query, 100))
        await _update_user_message(chat_request.messages, recreated_query)
        return recreated_query, True

    # LLM 재구성 실패 시: 되묻기 답변이면 원질문 + 답변 단순 결합으로 폴백
    if is_clarification and original_user_question:
        fallback_query = f"{user_message} {original_user_question}".strip()
        logger.warning(
            "[Query Recreation] LLM 실패 → 단순 결합 폴백: %s", shorten_text(fallback_query, 100)
        )
        await _update_user_message(chat_request.messages, fallback_query)
        return fallback_query, True

    logger.warning("[Query Recreation] 완성 질의 생성 실패, 원본 답변으로 계속 진행")
    return user_message, False


async def _handle_no_rag_mode(
    user_message: str,
    chat_request: ChatRequest,
) -> JSONResponse:
    """Handle NO-RAG mode response using system prompt only (non-streaming)."""
    logger.info("[NO-RAG Mode] 질문: %s", shorten_text(user_message, 100))
    response_message = await call_llm_api(
        message=user_message,
        system_prompt=load_system_prompt(),
        **_build_llm_kwargs(chat_request)
    )
    response = build_chat_response(
        response_message=response_message,
        user_message=user_message,
        model_name=Config.MODEL_NAME,
        conv_id=chat_request.conv_id,
    )
    return JSONResponse(content=response, status_code=200)


async def _handle_rag_mode(
    user_message: str,
    chat_request: ChatRequest,
    conv_id: str,
    stream: bool,
    intent: str = "general",
    sigun_filters: list = None,
    reformed_query: str = None,
    expanded_queries: list = None,
    keywords: list = None,
    llm_recommended_followup: bool = False,
    search_target: str | None = None,
    policy_priority_tag: str | None = None,
    excluded_chunk_ids: list | None = None,
    excluded_service_names: list | None = None,
    final_user_message: str | None = None,
    more_info: bool = False,
    more_detail: bool = False,
) -> JSONResponse | StreamingResponse:
    """Handle RAG mode response using query reform and retrieval."""
    from app.shared.utils.status_messages import build_status_message, STATUS_QUERY_REFORM
    from app.tts.service import request_tts_stream

    # 시군: 추천 후속(/recommended-question)도 check_sigun으로 확정(되묻기만 생략)
    if sigun_filters is None:
        checked_filters, need_sigun_clarify, sigun_ask_msg = check_sigun(
            user_message, chat_request.messages
        )
        if llm_recommended_followup:
            sigun_filters = checked_filters if checked_filters else None
            logger.info(
                "[SigunCheck/_handle_rag_mode] recommended_followup → sigun_filters=%s (되묻기 없음)",
                sigun_filters,
            )
        elif need_sigun_clarify:
            sigun_ask_count = count_sigun_ask_attempts(chat_request.messages)
            if sigun_ask_count >= MAX_SIGUN_ASK_ATTEMPTS:
                response = build_chat_response(
                    response_message=MSG_SIGUN_FAILURE,
                    user_message=user_message,
                    model_name=Config.MODEL_NAME,
                )
                return JSONResponse(content=response, status_code=200)
            logger.info(f"[SigunCheck/_handle_rag_mode] 시군 되묻기: {sigun_ask_msg!r}")
            return await _handle_clarify_response(
                user_message,
                sigun_ask_msg,
                chat_request,
                stream,
            )
        else:
            sigun_filters = checked_filters if checked_filters else None
            logger.info(f"[SigunCheck/_handle_rag_mode] sigun_filters={sigun_filters}")

    if stream:
        if llm_recommended_followup:
            reformed_query = user_message
        elif reformed_query is None:
            reformed_query = await reform_query_if_needed(
                user_message=user_message,
                messages=chat_request.messages,
            )
        logger.info(f"[RAG QUERY] {reformed_query}")

        async def rag_stream():
            yield f"data: {json.dumps({'conv_id': chat_request.conv_id}, ensure_ascii=False)}\n\n"
            if not llm_recommended_followup:
                yield build_status_message(STATUS_QUERY_REFORM)

            status_queue = asyncio.Queue()

            async def emit_status(message: str):
                await status_queue.put(message)

            rag_processor = _get_rag_processor(
                intent, recommended_question_route=llm_recommended_followup
            )
            _rag_stream_kw = dict(
                message=user_message,
                reformed_query=reformed_query,
                temperature=chat_request.temperature,
                max_tokens=chat_request.max_completion_tokens,
                stream=True,
                frequency_penalty=chat_request.frequency_penalty,
                repetition_penalty=getattr(chat_request, 'repetition_penalty', 1.0),
                top_p=chat_request.top_p,
                top_k=getattr(chat_request, 'top_k', 1),
                seed=chat_request.seed,
                tools=chat_request.tools,
                status_callback=emit_status,
                intent=intent,
                messages=chat_request.messages,
                sigun_filters=sigun_filters,
                precomputed_expanded_queries=expanded_queries,
                precomputed_keywords=keywords,
                precomputed_search_target=search_target,
                precomputed_policy_priority_tag=policy_priority_tag,
                excluded_chunk_ids=excluded_chunk_ids,
                excluded_service_names=excluded_service_names,
                final_user_message=final_user_message,
            )
            if intent == "general":
                _rag_stream_kw["more_detail"] = more_detail
            rag_task = asyncio.create_task(rag_processor(**_rag_stream_kw))

            async for status_msg in drain_status_until_done(rag_task, status_queue):
                yield build_status_message(status_msg)

            try:
                result, referenced_documents = await rag_task
            except Exception as rag_error:
                logger.error(f"[RAG Task Error] {rag_error}", exc_info=True)
                error_msg = "검색 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
                async for chunk in _stream_delta_content(error_msg):
                    yield chunk
                yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            referenced_documents = await asyncio.to_thread(_enrich_referenced_documents, referenced_documents)
            assistant_content = ""

            if isinstance(result, str):
                assistant_content = result
                raw_referenced_documents = list(referenced_documents or [])
                # 응답 본문 alias 매칭 필터 비활성화: LLM이 행정 접미사("지급/지원/사업")를
                # 생략한 경우 alias 매칭이 실패해 실제 참조 문서가 UI에서 누락되는 false negative
                # 가 발생함. RelevanceFilter(8b/sllm)가 이미 비관련 문서를 걸렀으므로 그 결과를
                # 그대로 UI 카드와 히스토리에 사용한다.
                filtered_referenced_documents = raw_referenced_documents
                persist_referenced_documents = raw_referenced_documents

                async for chunk in _stream_delta_content(assistant_content):
                    yield chunk
                if filtered_referenced_documents:
                    logger.info(f"[RAG Referenced Documents] Count: {len(filtered_referenced_documents)}, Docs: {[d.get('name', 'N/A') for d in filtered_referenced_documents]}")
                    # 문서 스니펫/내용 노출 방지: 상세 JSON 로그 비활성화
                    # logger.info(f"[RAG Referenced Documents JSON]\n{json.dumps(referenced_documents, ensure_ascii=False, indent=2)}")
                    yield f"data: {json.dumps({'referenced_documents': filtered_referenced_documents}, ensure_ascii=False)}\n\n"
                if intent == "guide_recommend" and assistant_content:
                    preprocess_payload = _build_assistant_preprocess_payload(
                        user_message=user_message,
                        intent=intent,
                        reformed_query=reformed_query,
                        expanded_queries=expanded_queries,
                        search_target=search_target,
                        policy_priority_tag=policy_priority_tag,
                        more_info=more_info,
                    )
                    await asyncio.to_thread(
                        partial(
                            _save_chat_history,
                            chat_request,
                            assistant_content,
                            user_message,
                            preprocess=preprocess_payload,
                            referenced_documents=persist_referenced_documents,
                        )
                    )
                yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}, ensure_ascii=False)}\n\n"
                yield f"data: [DONE]\n\n"
            else:
                async for chunk in result:
                    if isinstance(chunk, str) and chunk.startswith("data: "):
                        data_content = chunk[len("data: "):].strip()
                        if data_content in ("[DONE]", "[DONE]\n\n"):
                            raw_referenced_documents = list(referenced_documents or [])
                            # 응답 본문 alias 매칭 필터 비활성화 (위 동일 사유)
                            filtered_referenced_documents = raw_referenced_documents
                            persist_referenced_documents = raw_referenced_documents

                            if filtered_referenced_documents:
                                logger.info(f"[RAG Referenced Documents] Count: {len(filtered_referenced_documents)}, Docs: {[d.get('name', 'N/A') for d in filtered_referenced_documents]}")
                                yield f"data: {json.dumps({'referenced_documents': filtered_referenced_documents}, ensure_ascii=False)}\n\n"
                            if intent == "guide_recommend" and assistant_content:
                                preprocess_payload = _build_assistant_preprocess_payload(
                                    user_message=user_message,
                                    intent=intent,
                                    reformed_query=reformed_query,
                                    expanded_queries=expanded_queries,
                                    search_target=search_target,
                                    policy_priority_tag=policy_priority_tag,
                                    more_info=more_info,
                                )
                                await asyncio.to_thread(
                                    partial(
                                        _save_chat_history,
                                        chat_request,
                                        assistant_content,
                                        user_message,
                                        preprocess=preprocess_payload,
                                        referenced_documents=persist_referenced_documents,
                                    )
                                )
                            yield chunk
                            continue

                        try:
                            payload = json.loads(data_content)
                            delta = payload.get("choices", [{}])[0].get("delta", {})
                            content_piece = delta.get("content", "")
                            assistant_content += content_piece
                        except Exception:
                            pass

                    yield chunk

        return StreamingResponse(
            rag_stream(),
            media_type="text/event-stream",
            headers=SSE_RESPONSE_HEADERS,
        )
    else:
        if llm_recommended_followup:
            reformed_query = user_message
        elif reformed_query is None:
            reformed_query = await reform_query_if_needed(
                user_message=user_message,
                messages=chat_request.messages,
            )
        logger.info(f"[RAG QUERY] {reformed_query}")
        llm_kwargs = _build_llm_kwargs(chat_request)
        rag_processor = _get_rag_processor(
            intent, recommended_question_route=llm_recommended_followup
        )
        _rag_kw = dict(
            message=user_message,
            reformed_query=reformed_query,
            stream=False,
            intent=intent,
            messages=chat_request.messages,
            sigun_filters=sigun_filters,
            precomputed_expanded_queries=expanded_queries,
            precomputed_keywords=keywords,
            precomputed_search_target=search_target,
            precomputed_policy_priority_tag=policy_priority_tag,
            excluded_chunk_ids=excluded_chunk_ids,
            excluded_service_names=excluded_service_names,
            final_user_message=final_user_message,
            **{k: v for k, v in llm_kwargs.items() if k != "messages"},
        )
        if intent == "general":
            _rag_kw["more_detail"] = more_detail
        response_message, referenced_documents = await rag_processor(**_rag_kw)

        referenced_documents = await asyncio.to_thread(_enrich_referenced_documents, referenced_documents)
        raw_referenced_documents = list(referenced_documents or [])
        # 응답 본문 alias 매칭 필터 비활성화 (위 동일 사유)
        filtered_referenced_documents = raw_referenced_documents
        persist_referenced_documents = raw_referenced_documents

        preprocess_payload = _build_assistant_preprocess_payload(
            user_message=user_message,
            intent=intent,
            reformed_query=reformed_query,
            expanded_queries=expanded_queries,
            search_target=search_target,
            policy_priority_tag=policy_priority_tag,
            more_info=more_info,
        )
        conv_id = await asyncio.to_thread(
            partial(
                _save_chat_history,
                chat_request,
                response_message,
                user_message,
                preprocess=preprocess_payload,
                referenced_documents=persist_referenced_documents,
            )
        )

        if chat_request.input_type == "voice":
            tts_result = await request_tts_stream(text=response_message)
            response_message = tts_result.get("text", response_message)

        if filtered_referenced_documents:
            logger.info(f"[RAG Referenced Documents] Count: {len(filtered_referenced_documents)}, Docs: {[d.get('name', 'N/A') for d in filtered_referenced_documents]}")
            # 문서 스니펫/내용 노출 방지: 상세 JSON 로그 비활성화
            # logger.info(f"[RAG Referenced Documents JSON]\n{json.dumps(referenced_documents, ensure_ascii=False, indent=2)}")

        response = build_chat_response(
            response_message=response_message,
            user_message=user_message,
            model_name=Config.MODEL_NAME,
            referenced_documents=filtered_referenced_documents,
            conv_id=conv_id,
        )
        return JSONResponse(content=response, status_code=200)
