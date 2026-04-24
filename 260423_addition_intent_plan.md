# 260423 MORE_INFO 재검색 로직 구현 계획

## Context

사용자가 "더 알려줘" 같은 후속 질문을 했을 때, 이전에 응답한 문서를 제외하고
새로운 문서를 검색해 응답하는 기능 추가.

트리거 조건: 이전 assistant 응답이 general / comparison / guide_recommend / search 의도로 생성된 경우.
적용 범위: OKMS 계열 컬렉션(`GSND_BIZ_DATASET_V4`, `GOV_OKMS_V1`)만 제외 필터 적용.

**핵심 발견**: `excluded_chunk_ids` / `excluded_service_names` 파라미터가 4개 RAG 프로세서
(`process_rag_general`, `process_rag_with_documents_v2`, `process_rag_guide_recommend`, `process_rag_search`)에
이미 구현되어 있고 `filter_excluded_docs` (`services_v2/common.py:82`)도 완성 상태.
현재는 이 파라미터들이 항상 `None`으로 전달됨 — 연결만 하면 동작함.

---

## 작업 범위 (파일별)

### 신규 생성 (1개)
- `prompts/next_intent_prompt.txt`

### 수정 (4개)
1. `utils/prompt_loader.py` — `load_next_intent_prompt()` 추가
2. `services/router_service.py` — `classify_next_intent()` 함수 추가
3. `routers/deps.py` — `_save_chat_history()` 메타데이터 저장 확장
4. `routers/chat/helpers.py` — `_handle_rag_mode()` MORE_INFO 분기 추가

### LLM 메시지 메타데이터 제거 (2개)
5. `services_v2/response_generator.py` — history 빌드 시 `role`+`content`만 사용
6. `services/rag_service/response.py:132` — history 빌드 시 동일 처리

---

## Step 1 — `prompts/next_intent_prompt.txt` 신규 생성

목적: 이전 대화 + 현재 질문 → `intent` + `re_query` JSON 반환

```
당신은 사용자의 후속 질문 의도를 분류하는 AI입니다.

이전 대화 내역과 현재 질문을 보고, 사용자가 이전 답변에서 받지 못한
복지 서비스 정보를 더 원하는지(MORE_INFO), 아니면 다른 주제의 질문인지(OTHER) 판단합니다.

[MORE_INFO 기준]
- "더 알려줘", "더 있어?", "다른 것도", "추가로", "또 없어?", "더 많이" 등
- 이전 답변 주제를 유지하면서 더 많은 정보를 요청하는 경우
- 이미 받은 서비스 외에 다른 서비스를 요청하는 경우

[OTHER 기준]
- 완전히 새로운 주제 또는 조건이 변경된 질문 (지역·나이·생애주기 변경 등)
- 특정 서비스의 세부 내용을 묻는 경우 (이미 받은 정보에 대한 상세 질문)
- 비교, 추천 등 다른 유형의 요청

[출력 형식] JSON만 출력 (설명 없이):
{
  "intent": "MORE_INFO",
  "re_query": "이전 대화 주제 + 현재 요청을 통합한 검색용 완성 질의"
}
또는
{
  "intent": "OTHER",
  "re_query": "현재 질문 기반 검색용 완성 질의"
}

re_query 작성 규칙:
- MORE_INFO: 이전 대화의 핵심 조건(지역, 생애주기 등) + 현재 질문 통합
- OTHER: 현재 질문을 Mariner 검색에 최적화된 형태로 재작성
- 반드시 한국어로 작성
```

---

## Step 2 — `utils/prompt_loader.py` 수정

기존 `load_classification_general_prompt` 등과 동일한 패턴으로 추가:

```python
def load_next_intent_prompt() -> str:
    return _load_prompt("next_intent_prompt.txt")
```

**참고 파일**: `utils/prompt_loader.py` (기존 함수 패턴 확인 후 동일하게 작성)

---

## Step 3 — `services/router_service.py` 수정

`extract_triples` 함수 아래에 추가:

```python
async def classify_next_intent(messages: list, current_query: str) -> dict:
    """
    이전 대화 + 현재 질문 → MORE_INFO 여부 + re_query 분류

    Returns:
        {"intent": "MORE_INFO" | "OTHER", "re_query": str}
    실패 시: {"intent": "OTHER", "re_query": current_query}
    """
    from utils.prompt_loader import load_next_intent_prompt
    import json as _json

    try:
        prompt = load_next_intent_prompt()
        if not prompt:
            return {"intent": "OTHER", "re_query": current_query}

        # 이전 대화 히스토리 + 현재 질문 구성 (role/content만 추출)
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in (messages or [])
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        history.append({"role": ROLE_USER, "content": current_query})

        response = await call_llm_api(
            messages=history,
            extra_system_prompts=[prompt],
            temperature=0.0,
            max_tokens=256,
        )

        # 문자열 응답에서 JSON 추출
        text = response if isinstance(response, str) else ""
        match = re.search(r'\{.*?\}', text, re.DOTALL)
        if match:
            result = _json.loads(match.group())
            intent = result.get("intent", "OTHER")
            re_query = result.get("re_query", current_query) or current_query
            logger.info("[classify_next_intent] intent=%s, re_query=%s", intent, shorten_text(re_query, 80))
            return {"intent": intent, "re_query": re_query}
    except Exception as e:
        logger.warning("[classify_next_intent] 분류 실패: %s", e)

    return {"intent": "OTHER", "re_query": current_query}
```

**import 추가 필요**: `import re` (이미 있을 가능성 높음), `from utils.helpers import shorten_text` (이미 있음)

---

## Step 4 — `routers/deps.py` 수정

### 4-1. `_save_chat_history()` 시그니처 확장 (line 212)

현재:
```python
def _save_chat_history(chat_request, assistant_message, processed_user_message=None, **kwargs):
```

변경 후:
```python
def _save_chat_history(
    chat_request,
    assistant_message,
    processed_user_message=None,
    referenced_document_ids: list = None,
    rag_intent: str = None,
    referenced_service_names: list = None,
    **kwargs
):
```

### 4-2. assistant 메시지 저장 시 메타데이터 포함 (line 253)

현재:
```python
messages.append({"role": "assistant", "content": assistant_message})
```

변경 후:
```python
assistant_msg = {"role": "assistant", "content": assistant_message}
if referenced_document_ids is not None:
    assistant_msg["referenced_document_ids"] = referenced_document_ids
if rag_intent is not None:
    assistant_msg["rag_intent"] = rag_intent
if referenced_service_names is not None:
    assistant_msg["referenced_service_names"] = referenced_service_names
messages.append(assistant_msg)
```

덮어쓰기 분기(line 249-251)도 동일하게:
```python
messages[-1]["content"] = assistant_message
if referenced_document_ids is not None:
    messages[-1]["referenced_document_ids"] = referenced_document_ids
if rag_intent is not None:
    messages[-1]["rag_intent"] = rag_intent
if referenced_service_names is not None:
    messages[-1]["referenced_service_names"] = referenced_service_names
```

---

## Step 5 — `routers/chat/helpers.py` 수정 (핵심)

### 5-1. 임포트 추가

```python
from services.router_service import classify_next_intent
from services.chat_history_service import get_chat_history_service
```

### 5-2. 헬퍼 함수 추가 (파일 상단 상수/함수 영역에)

```python
_MORE_INFO_RAG_INTENTS = {"general", "comparison", "guide_recommend", "search"}


def _extract_excluded_ids_from_messages(messages: list) -> tuple[list, list]:
    """
    messages에서 마지막 RAG assistant 메시지의 메타데이터 추출.
    Returns: (excluded_chunk_ids, excluded_service_names)
    """
    for msg in reversed(messages or []):
        if msg.get("role") != "assistant":
            continue
        if msg.get("rag_intent") not in _MORE_INFO_RAG_INTENTS:
            continue
        ids = msg.get("referenced_document_ids") or []
        names = msg.get("referenced_service_names") or []
        return ids, names
    return [], []
```

### 5-3. `_handle_rag_mode()` 수정 — NON-STREAMING (line 339-380)

`reformed_query` 설정 직후, `rag_processor` 호출 전에 삽입:

```python
# ── MORE_INFO 감지 ─────────────────────────────────────────────────────────
excluded_chunk_ids = None
excluded_service_names = None

# 1단계: frontend messages에서 메타데이터 시도
_excl_ids, _excl_names = _extract_excluded_ids_from_messages(chat_request.messages)

# 2단계: 없으면 DB에서 조회
if not _excl_ids and chat_request.conv_id:
    try:
        _hist = await asyncio.to_thread(
            get_chat_history_service(Config).get_history, chat_request.conv_id
        )
        _excl_ids, _excl_names = _extract_excluded_ids_from_messages(_hist)
    except Exception as _e:
        logger.warning("[MoreInfo] DB 조회 실패: %s", _e)

if _excl_ids:
    _next = await classify_next_intent(chat_request.messages, user_message)
    if _next["intent"] == "MORE_INFO":
        excluded_chunk_ids = _excl_ids
        excluded_service_names = _excl_names or None
        reformed_query = _next["re_query"]
        expanded_queries = None   # 재계산 강제
        keywords = None
        logger.info("[MoreInfo] MORE_INFO 감지 - excl=%d개, re_query=%s",
                    len(excluded_chunk_ids), reformed_query[:60])
# ──────────────────────────────────────────────────────────────────────────

response_message, referenced_documents = await rag_processor(
    message=user_message,
    reformed_query=reformed_query,
    stream=False,
    intent=intent,
    messages=chat_request.messages,
    sigun_filters=sigun_filters,
    precomputed_expanded_queries=expanded_queries,
    precomputed_keywords=keywords,
    excluded_chunk_ids=excluded_chunk_ids,          # 추가
    excluded_service_names=excluded_service_names,  # 추가
    **{k: v for k, v in llm_kwargs.items() if k != "messages"}
)

# 메타데이터 포함 저장
_ref_ids = [d.get("chunk_id") or d.get("id", "") for d in (referenced_documents or [])]
_ref_names = [d.get("name", "") for d in (referenced_documents or [])]
conv_id = await asyncio.to_thread(
    _save_chat_history, chat_request, response_message, user_message,
    referenced_document_ids=_ref_ids,
    rag_intent=intent,
    referenced_service_names=_ref_names,
)
```

### 5-4. STREAMING 경로 수정

스트리밍 시작 전 동일한 MORE_INFO 감지 블록 삽입.
`rag_task` 생성 시 `excluded_chunk_ids`, `excluded_service_names` 전달.
스트림 완료 후 `_save_chat_history` 호출을 guide_recommend에서 모든 intent로 확장:

```python
# 기존 (guide_recommend만 저장)
if intent == "guide_recommend" and assistant_content:
    await asyncio.to_thread(_save_chat_history, chat_request, assistant_content, user_message)

# 변경 후 (모든 intent 저장 + 메타데이터)
if assistant_content:
    _ref_ids = [d.get("chunk_id") or d.get("id", "") for d in (referenced_documents or [])]
    _ref_names = [d.get("name", "") for d in (referenced_documents or [])]
    await asyncio.to_thread(
        _save_chat_history, chat_request, assistant_content, user_message,
        referenced_document_ids=_ref_ids,
        rag_intent=intent,
        referenced_service_names=_ref_names,
    )
```

스트리밍에는 동일한 패턴이 2곳에 있음 (str 응답 경로 / streaming 응답 경로) — 둘 다 수정.

---

## Step 6 — LLM 메시지 메타데이터 제거

LLM API 전송 전 history에서 `role`+`content`만 전달되도록 수정.

### `services_v2/response_generator.py`

history 빌드 라인 찾아서:
```python
# 변경 전
history = [m for m in messages[:-1] if m.get("role") in ("user", "assistant")]

# 변경 후
history = [
    {"role": m["role"], "content": m.get("content", "")}
    for m in messages[:-1]
    if m.get("role") in ("user", "assistant")
]
```

### `services/rag_service/response.py:132`

```python
# 변경 전
history = [m for m in messages[:-1] if m.get("role") in (ROLE_USER, "assistant")]

# 변경 후
history = [
    {"role": m["role"], "content": m.get("content", "")}
    for m in messages[:-1]
    if m.get("role") in (ROLE_USER, "assistant")
]
```

---

## 데이터 흐름 요약

```
[요청 도착]
  chat_request.messages
       ↓
[_handle_rag_mode]
  1. _extract_excluded_ids_from_messages(messages) 시도
  2. 메타데이터 없으면 get_history(conv_id) DB 조회
  3. excluded_ids 있으면 classify_next_intent() 호출
       ↓
  [MORE_INFO == True]
    reformed_query = re_query
    excluded_chunk_ids = [이전 chunk_ids]
    excluded_service_names = [이전 서비스명]
    precomputed 캐시 무효화 (None으로 설정)
       ↓
[rag_processor(..., excluded_chunk_ids=..., excluded_service_names=...)]
  → filter_excluded_docs() 내부에서 제외 적용 (이미 구현됨 — services_v2/common.py:82)
       ↓
[_save_chat_history(..., referenced_document_ids=..., rag_intent=...)]
  → DB에 메타데이터 저장 → 다음 턴에서 조회 가능
```

---

## 주의사항

1. **스트리밍 저장 확장**: 현재 `guide_recommend`만 `_save_chat_history` 호출 — 모든 intent로 확장 필수
2. **`deps.py` `already_saved` 체크** (line 241-248): 메타데이터 필드 추가 후 중복 저장 방지 로직 동작 확인
3. **re_query 처리**: `reformed_query=re_query`, `precomputed_*=None` 설정 시 각 RAG 프로세서 내부에서 `expand_query(re_query)`, `extract_triples(re_query)` 자동 실행됨
4. **GOV_OKMS_V1**: `mariner_v2/queryset_gov_okms.py`에는 `excluded_chunk_ids` 파라미터 없음 — OKMS 후처리(`filter_excluded_docs`)로만 제외됨 (Mariner 쿼리 레벨 제외 불필요)
5. **`router_service.py` import**: `import re`가 이미 있는지 확인 후 없으면 추가

---

## 검증 방법

1. **단위 검증**: `classify_next_intent()` 직접 호출
   ```python
   messages = [
       {"role": "user", "content": "노인 복지 서비스 알려줘"},
       {"role": "assistant", "content": "...", "rag_intent": "general",
        "referenced_document_ids": ["CHUNK001", "CHUNK002"]},
   ]
   result = await classify_next_intent(messages, "더 알려줘")
   # 기대: {"intent": "MORE_INFO", "re_query": "노인 복지 서비스 추가 정보"}
   ```

2. **통합 검증** (Mariner 서버 접근 환경):
   - 1차: "경상남도 노인 복지서비스 알려줘" → referenced_documents 확인
   - 2차: "더 알려줘" → 1차와 다른 문서가 반환되는지 확인

3. **메타데이터 저장 확인**: DB `messages_json` 조회 → assistant 메시지에 `rag_intent`, `referenced_document_ids` 필드 존재 확인

4. **LLM 전송 로그**: `[Final Response] messages:` 로그에서 메타데이터 필드 없는지 확인

---

## 관련 파일 경로 정리

| 역할 | 파일 |
|------|------|
| 신규 프롬프트 | `prompts/next_intent_prompt.txt` |
| 프롬프트 로더 | `utils/prompt_loader.py` |
| 의도 분류 함수 | `services/router_service.py` |
| 대화 저장 | `routers/deps.py` |
| RAG 진입점 (핵심) | `routers/chat/helpers.py` |
| LLM 메시지 정제 (v2) | `services_v2/response_generator.py` |
| LLM 메시지 정제 (v1) | `services/rag_service/response.py` |
| 제외 필터 (이미 구현) | `services_v2/common.py:82` |
| general 프로세서 | `services_v2/rag_general/pipeline.py` |
| comparison 프로세서 | `services_v2/rag_comparison/pipeline.py` |
| guide_recommend 프로세서 | `services_v2/rag_guide_recommend/pipeline.py` |
| search 프로세서 | `services_v2/rag_search.py` |
