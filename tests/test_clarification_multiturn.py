"""되묻기 답변(is_clarification=True) 멀티턴 처리 회귀 테스트.

시나리오: 사용자가 "장애수당 알려줘"라고 묻고 봇이 시군 되묻기를 한 뒤
사용자가 "양산"이라고 답한 경우, 다음을 보장한다.

1) 스트리밍 경로(`_streaming_chat_flow`):
   - `_resolve_more_results_context`(따라서 `classify_next_intent`)는 호출되지 않는다.
   - `_run_query_recreation`이 원질문/되묻기 답변을 결합하기 위해 호출된다.

2) 비스트리밍 라우터(`router._chat_completions_core`):
   - `classify_next_intent`는 호출되지 않는다.
   - `_run_query_recreation`이 호출된다.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CLARIFICATION_MESSAGES = [
    {"role": "user", "content": "장애수당 알려줘"},
    {
        "role": "assistant",
        "content": "경상남도 내 시/군을 알려주세요. 어느 지역에 거주하고 계신가요?",
    },
    {"role": "user", "content": "양산"},
]


# ---------------------------------------------------------------------------
# 스트리밍 경로
# ---------------------------------------------------------------------------


class _StopFlow(Exception):
    """테스트가 더 이상 진행할 필요 없음을 표시."""


@pytest.mark.asyncio
async def test_streaming_clarification_skips_more_results_and_runs_recreation():
    from app.shared.schemas import ChatRequest
    from app.chat import _streaming
    from app.chat._pipeline_steps import PreCheckResult, SigunCheckResult

    chat_request = ChatRequest(
        user_id="user-test",
        conv_id="conv-test",
        messages=list(CLARIFICATION_MESSAGES),
        stream=True,
    )

    captured: dict = {}

    async def _fake_run_query_recreation(user_message, chat_req, is_clar):
        captured["user_message"] = user_message
        captured["is_clarification"] = is_clar
        captured["history"] = list(chat_req.messages)
        # 더 이상 진행할 필요가 없으므로 흐름을 끊는다.
        raise _StopFlow()

    early_sigun_result = SigunCheckResult(
        filters=["경상남도 양산시"], need_clarify=False, ask_message=""
    )

    forbidden_resolver = AsyncMock(
        side_effect=AssertionError("is_clarification=True에서 _resolve_more_results_context 호출 금지")
    )

    with (
        patch.object(_streaming, "set_log_context", return_value=None),
        patch.object(_streaming, "reset_log_context"),
        patch.object(_streaming, "_resolve_more_results_context", new=forbidden_resolver),
        patch.object(
            _streaming,
            "run_pre_check",
            new=AsyncMock(return_value=PreCheckResult(use_rag=True, clarification_question="")),
        ),
        patch.object(
            _streaming,
            "run_early_sigun_check",
            return_value=early_sigun_result,
        ),
        patch.object(
            _streaming,
            "_run_query_recreation",
            new=AsyncMock(side_effect=_fake_run_query_recreation),
        ),
        patch.object(_streaming, "_write_timing_csv"),
    ):
        gen = _streaming._streaming_chat_flow(
            chat_request,
            user_message="양산",
            original_user_message="양산",
            is_clarification=True,
        )

        with pytest.raises(_StopFlow):
            async for _ in gen:
                pass

    forbidden_resolver.assert_not_awaited()
    assert captured["is_clarification"] is True
    assert captured["user_message"] == "양산"
    # _run_query_recreation 내부에서 두 번째 user 메시지를 원질문으로, 마지막 user를 답변으로 사용
    assert captured["history"][0]["content"] == "장애수당 알려줘"
    assert captured["history"][-1]["content"] == "양산"


# ---------------------------------------------------------------------------
# 비스트리밍 라우터 경로
# ---------------------------------------------------------------------------


class _StopRouter(BaseException):
    """라우터의 광범위한 except Exception 에 잡히지 않도록 BaseException 상속."""


@pytest.mark.asyncio
async def test_router_clarification_skips_classify_next_intent():
    """비스트리밍 _chat_completions_core 에서 classify_next_intent 가 호출되지 않아야 함."""
    from app.chat import router as chat_router

    forbidden_classifier = AsyncMock(
        side_effect=AssertionError("is_clarification=True에서 classify_next_intent 호출 금지"),
    )

    captured: dict = {}

    async def _fake_run_query_recreation(user_message, chat_req, is_clar):
        captured["called"] = True
        captured["user_message"] = user_message
        captured["is_clarification"] = is_clar
        raise _StopRouter()

    request = MagicMock()
    request.headers = {"origin": "test"}

    async def _json():
        return {
            "user_id": "user-test",
            "conv_id": "conv-test",
            "messages": list(CLARIFICATION_MESSAGES),
            "stream": False,
        }

    request.json = _json

    with (
        patch.object(chat_router, "set_log_context", return_value=None),
        patch.object(chat_router, "reset_log_context"),
        patch.object(chat_router, "_validate_request", return_value=(True, None)),
        patch.object(
            chat_router,
            "_validate_user_message",
            return_value=(True, None, "양산"),
        ),
        patch.object(
            chat_router, "_merge_and_init_conversation", new=AsyncMock(return_value=None)
        ),
        patch.object(chat_router, "classify_next_intent", new=forbidden_classifier),
        patch.object(
            chat_router,
            "_run_query_recreation",
            new=AsyncMock(side_effect=_fake_run_query_recreation),
        ),
    ):
        # is_clarification_answer 는 messages 의 직전 assistant 가 '?' 로 끝나면 True 를 돌려준다.
        # CLARIFICATION_MESSAGES 가 그 조건을 만족하므로 별도 패치 불필요.
        with pytest.raises(_StopRouter):
            await chat_router._chat_completions_core(request, llm_recommended_followup=False)

    forbidden_classifier.assert_not_awaited()
    assert captured.get("called") is True
    assert captured["is_clarification"] is True


# ---------------------------------------------------------------------------
# 회귀 가드: is_clarification=False 일 때는 더알려줘 분기가 여전히 동작
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_non_clarification_still_invokes_more_results():
    """일반 turn(is_clarification=False)에서는 _resolve_more_results_context 호출되어야 함."""
    from app.shared.schemas import ChatRequest
    from app.chat import _streaming
    from app.chat._streaming import _MoreResultsContext

    chat_request = ChatRequest(
        user_id="user-test",
        conv_id="conv-test",
        messages=[{"role": "user", "content": "장애수당 알려줘"}],
        stream=True,
    )

    resolver_mock = AsyncMock(side_effect=_StopFlow())

    with (
        patch.object(_streaming, "set_log_context", return_value=None),
        patch.object(_streaming, "reset_log_context"),
        patch.object(_streaming, "_resolve_more_results_context", new=resolver_mock),
        patch.object(_streaming, "_write_timing_csv"),
    ):
        gen = _streaming._streaming_chat_flow(
            chat_request,
            user_message="장애수당 알려줘",
            original_user_message="장애수당 알려줘",
            is_clarification=False,
        )

        with pytest.raises(_StopFlow):
            async for _ in gen:
                pass

    resolver_mock.assert_awaited_once()
