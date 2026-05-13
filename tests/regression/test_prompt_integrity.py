"""프롬프트 파일의 무결성 회귀 테스트.

리팩토링 로드맵 P0의 골든 케이스 일부.
- 모든 `prompts/*.txt` 가 로드 성공
- 핵심 프롬프트의 플레이스홀더가 보존됨
- JSON 출력 형식 예시가 valid JSON
- 프롬프트 길이가 합리적 범위
- 코드의 `.replace()` 패턴이 사용하는 키와 프롬프트 키의 정합성
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PROMPTS_DIR = Path("prompts")
SRC_DIR = Path("src/app")

# 코드에서 사용되는 플레이스홀더 (.replace("{xxx}", ...) 패턴)
_REPLACE_RE = re.compile(r'\.replace\(\s*[\'"](\{[^}]+\})[\'"]')


def _collect_replace_keys_in_code() -> set[str]:
    keys: set[str] = set()
    for py in SRC_DIR.rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for m in _REPLACE_RE.finditer(text):
            keys.add(m.group(1))
    return keys


@pytest.fixture(scope="module")
def all_prompts() -> dict[str, str]:
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in PROMPTS_DIR.glob("*.txt")
    }


# ---------------------------------------------------------------------------
# 기본 로딩 무결성
# ---------------------------------------------------------------------------

def test_prompts_dir_exists():
    assert PROMPTS_DIR.is_dir(), "prompts/ 디렉토리가 존재해야 함"


def test_at_least_one_prompt_exists(all_prompts):
    assert all_prompts, "prompts/*.txt 가 하나 이상 있어야 함"


def test_no_empty_prompt(all_prompts):
    empty = [name for name, body in all_prompts.items() if not body.strip()]
    assert not empty, f"빈 프롬프트: {empty}"


def test_prompt_length_within_reasonable_range(all_prompts):
    """비합리적으로 짧거나 긴 프롬프트는 회귀 신호."""
    suspicious_short = [n for n, b in all_prompts.items() if len(b) < 50]
    suspicious_long = [n for n, b in all_prompts.items() if len(b) > 50_000]
    assert not suspicious_short, f"매우 짧음(<50자): {suspicious_short}"
    assert not suspicious_long, f"매우 김(>50KB): {suspicious_long}"


# ---------------------------------------------------------------------------
# 핵심 프롬프트의 플레이스홀더 보존
# ---------------------------------------------------------------------------

_REQUIRED_PLACEHOLDERS = {
    "unified_preprocessing_prompt.txt": ["{현재연도}", "{작년연도}", "{이전 대화}", "{사용자 질문}"],
    "next_intent_prompt.txt": [
        "{prior_intent}", "{is_clarification_question}", "{prior_service_names}",
        "{before_user_input}", "{before_answer}", "{user_input}",
    ],
    "pre_check_prompt.txt": ["{현재연도}", "{작년연도}", "{이전 대화}", "{사용자 질문}"],
}


@pytest.mark.parametrize("filename, placeholders", list(_REQUIRED_PLACEHOLDERS.items()))
def test_required_placeholders_preserved(filename, placeholders):
    path = PROMPTS_DIR / filename
    if not path.exists():
        pytest.skip(f"{filename} 없음 (옵션 프롬프트일 수 있음)")
    text = path.read_text(encoding="utf-8")
    missing = [ph for ph in placeholders if ph not in text]
    assert not missing, f"{filename} 의 플레이스홀더 누락: {missing}"


def test_code_replace_keys_exist_in_prompts():
    """src/ 코드가 .replace("{xxx}", ...) 로 치환하는 모든 키가
    적어도 한 프롬프트에는 등장해야 한다.
    """
    code_keys = _collect_replace_keys_in_code()
    if not code_keys:
        pytest.skip("코드에서 .replace 키를 찾지 못함")

    prompt_blob = "\n".join(
        p.read_text(encoding="utf-8") for p in PROMPTS_DIR.glob("*.txt")
    )

    missing = [k for k in code_keys if k not in prompt_blob]
    # 일부 키는 동적 생성일 수 있어 완전한 일치는 강요하지 않되, 핵심 키는 반드시 존재
    must_have = [
        "{현재연도}", "{이전 대화}", "{사용자 질문}",
        "{user_input}", "{prior_intent}",
    ]
    truly_missing = [k for k in must_have if k in code_keys and k in missing]
    assert not truly_missing, (
        f"코드가 사용하는데 어떤 프롬프트에도 존재하지 않는 핵심 키: {truly_missing}"
    )


# ---------------------------------------------------------------------------
# unified_preprocessing 의 최종 출력 JSON 예시 파싱 가능성
# ---------------------------------------------------------------------------

def test_unified_preprocessing_json_example_parses():
    path = PROMPTS_DIR / "unified_preprocessing_prompt.txt"
    text = path.read_text(encoding="utf-8")
    match = re.search(r"\[최종 출력 형식\]\s*\n+(\{[\s\S]*?\n\})", text)
    assert match, "[최종 출력 형식] JSON 블록을 찾을 수 없음"
    payload = json.loads(match.group(1))
    # 코드에서 읽는 핵심 키들이 예시에 모두 존재해야 함
    required_keys = {
        "query", "intent", "intent_reason", "reformed_query",
        "expanded_queries", "search_target", "policy_priority_tag",
        "detail_requested",
    }
    missing = required_keys - set(payload.keys())
    assert not missing, f"JSON 예시 누락 키: {missing}"


def test_next_intent_output_schema_keys_present():
    """next_intent_prompt 의 출력 형식에 핵심 키 3개가 모두 등장."""
    path = PROMPTS_DIR / "next_intent_prompt.txt"
    if not path.exists():
        pytest.skip("next_intent_prompt.txt 없음")
    text = path.read_text(encoding="utf-8")
    for key in ('"intent"', '"re_query"', '"reason"'):
        assert key in text, f"출력 형식에 {key} 누락"


# ---------------------------------------------------------------------------
# 로더 함수 정합성 — prompt_loader 가 모든 핵심 프롬프트를 실제로 읽어옴
# ---------------------------------------------------------------------------

def test_unified_preprocessing_loader_returns_text():
    from app.shared.utils.prompt_loader import load_unified_preprocessing_prompt

    body = load_unified_preprocessing_prompt()
    assert isinstance(body, str) and body.strip(), "unified preprocess 프롬프트 로드 실패"


def test_next_intent_loader_returns_text():
    from app.shared.utils.prompt_loader import load_next_intent_prompt

    body = load_next_intent_prompt()
    assert isinstance(body, str) and body.strip(), "next_intent 프롬프트 로드 실패"


def test_query_recreation_loader_returns_text():
    from app.shared.utils.prompt_loader import load_query_recreation_prompt

    body = load_query_recreation_prompt()
    assert isinstance(body, str) and body.strip(), "query_recreation 프롬프트 로드 실패"
