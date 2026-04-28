"""
SuggestQuestionsService — 추천 질문 생성 (Phase 2: 생성자 주입).

LLMClientProtocol을 생성자로 받아 call_llm_api를 주입 클라이언트로 실행.
테스트 시 Mock 클라이언트를 주입하거나 서비스 자체를 dependency_overrides로 교체.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from app.core.config import Config
from app.core.constants import ROLE_USER, ROLE_ASSISTANT
from app.core.protocols import LLMClientProtocol, SuggestQuestionsServiceProtocol
from app.shared.utils.prompt_loader import load_suggest_questions_prompt

logger = logging.getLogger(__name__)


class SuggestQuestionsService:
    """
    추천 질문 생성 서비스.

    Attributes:
        _client: LLMClientProtocol을 구현한 httpx.AsyncClient (또는 Mock)
    """

    def __init__(self, client: LLMClientProtocol) -> None:
        self._client = client

    async def generate(
        self,
        user_query: str,
        assistant_response: str,
        max_questions: int = 5,
    ) -> list[str]:
        """
        user_query + assistant_response 기반 추천 질문 생성.

        Returns:
            질문 문자열 리스트 (최대 max_questions개)
        """
        from app.chat.infra.llm.core import call_llm_api

        prompt = load_suggest_questions_prompt()
        if not prompt:
            logger.warning("[SuggestQuestions] 프롬프트 로드 실패")
            return []

        call_messages = [
            {"role": ROLE_USER,      "content": user_query},
            {"role": ROLE_ASSISTANT, "content": assistant_response},
            {"role": ROLE_USER,      "content": "위 대화를 바탕으로 관련성 있는 후속 질문 3개를 추천해주세요."},
        ]

        try:
            raw = await call_llm_api(
                temperature=0,
                messages=call_messages,
                extra_system_prompts=[prompt],
                response_format={"type": "json_object"},
                http_client=self._client,
            )
        except Exception:
            logger.exception("[SuggestQuestions] LLM 호출 실패")
            return []

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[SuggestQuestions] JSON 파싱 실패: %s", raw[:200])
            return []

        if isinstance(result, list):
            questions = result
        elif isinstance(result, dict):
            for key in ("questions", "suggested_questions", "추천_질문", "추천질문", "후속_질문"):
                if key in result:
                    questions = result[key]
                    break
            else:
                questions = next((v for v in result.values() if isinstance(v, list)), [])
        else:
            questions = []

        questions = [q for q in questions if isinstance(q, str) and q.strip()]
        questions = questions[:max_questions]
        logger.info("[SuggestQuestions] 생성된 추천 질문 %d개", len(questions))
        return questions


# --------------------------------------------------------------------------- #
# SuggestQuestionsServiceProtocol 준수 검증 (런타임)
# --------------------------------------------------------------------------- #
assert isinstance(SuggestQuestionsService(None), SuggestQuestionsServiceProtocol), (
    "SuggestQuestionsService must satisfy SuggestQuestionsServiceProtocol"
)
