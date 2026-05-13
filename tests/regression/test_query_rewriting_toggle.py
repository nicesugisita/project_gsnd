"""Query Rewriting 토글 (Config.QUERY_REWRITING_ENABLED) 회귀 테스트.

LLM 응답은 mock 으로 대체. config 분기 동작과 expanded_queries 강제 단축만 검증.
실제 프롬프트 품질은 LLM eval 셋으로 별도 실측 필요.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


def _mock_llm_response(*, expanded_count: int = 5) -> str:
    """unified_preprocess LLM 응답 mock JSON.

    expanded_count 만큼 expanded_queries 를 채워 반환.
    """
    base = {
        "query": "창원 노인 복지 추천",
        "intent": "guide_recommend",
        "intent_reason": "노인 복지 탐색",
        "reformed_query": "창원 노인 복지 추천",
        "expanded_queries": [
            f"창원 노인 복지 추천 변형{i}" for i in range(1, expanded_count + 1)
        ],
        "search_target": None,
        "policy_priority_tag": "elderly_benefits",
        "detail_requested": False,
    }
    import json
    return json.dumps(base, ensure_ascii=False)


@pytest.mark.asyncio
async def test_expand_mode_keeps_multiple_expansions(monkeypatch: pytest.MonkeyPatch) -> None:
    """QUERY_REWRITING_ENABLED=False(기본) — LLM 이 반환한 expansion 다수 유지."""
    from app.chat import preprocessing

    monkeypatch.setattr(preprocessing.Config, "QUERY_REWRITING_ENABLED", False)

    with patch.object(
        preprocessing, "call_llm_api", return_value=_mock_llm_response(expanded_count=5)
    ), patch.object(
        preprocessing, "load_unified_preprocessing_prompt", return_value="dummy {사용자 질문}"
    ):
        result = await preprocessing.unified_preprocess(
            user_query="창원 노인 복지 추천해줘",
            messages=[],
            use_rag=True,
        )

    # expand 모드: 다중 expanded_queries 유지 (5개)
    assert len(result["expanded_queries"]) > 1, (
        f"expand 모드는 다중 쿼리 유지해야 함, got={result['expanded_queries']}"
    )
    # reformed_query 외에 변형 쿼리가 들어 있어야 함
    assert result["expanded_queries"] != [result["reformed_query"]]


@pytest.mark.asyncio
async def test_rewrite_mode_forces_single_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """QUERY_REWRITING_ENABLED=True — LLM 이 5개 반환해도 [reformed_query] 단일 원소로 강제."""
    from app.chat import preprocessing

    monkeypatch.setattr(preprocessing.Config, "QUERY_REWRITING_ENABLED", True)

    with patch.object(
        preprocessing, "call_llm_api", return_value=_mock_llm_response(expanded_count=5)
    ), patch.object(
        preprocessing, "load_unified_preprocessing_prompt", return_value="dummy {사용자 질문}"
    ):
        result = await preprocessing.unified_preprocess(
            user_query="창원 노인 복지 추천해줘",
            messages=[],
            use_rag=True,
        )

    # rewrite 모드: expanded_queries 는 항상 단일 원소
    assert len(result["expanded_queries"]) == 1, (
        f"rewrite 모드는 단일 쿼리만 사용해야 함, got={result['expanded_queries']}"
    )
    assert result["expanded_queries"][0] == result["reformed_query"], (
        f"rewrite 모드는 expanded_queries[0] == reformed_query 보장, "
        f"got expanded={result['expanded_queries']}, reformed={result['reformed_query']}"
    )


def test_prompt_loader_branches_by_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """prompt_loader 가 config 토글에 따라 다른 파일을 로드."""
    from app.shared.utils import prompt_loader
    from app.core.config import Config

    loaded_files: list[str] = []

    def _fake_load(filename: str, default: str = "") -> str:
        loaded_files.append(filename)
        return f"<<{filename}>>"

    monkeypatch.setattr(prompt_loader, "_load_prompt_file", _fake_load)

    monkeypatch.setattr(Config, "QUERY_REWRITING_ENABLED", False)
    prompt_loader.load_unified_preprocessing_prompt()
    assert loaded_files[-1] == "unified_preprocessing_prompt.txt"

    monkeypatch.setattr(Config, "QUERY_REWRITING_ENABLED", True)
    prompt_loader.load_unified_preprocessing_prompt()
    assert loaded_files[-1] == "unified_preprocessing_prompt_rewrite.txt"
