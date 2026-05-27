# `policy_priority_tag` 룰 기반 fallback 도입 핸드오프

> **작업 목적**: 같은 질문이 매 호출마다 다른 검색 결과를 내는 비결정성을 줄인다.
> 가장 큰 변동 요인이 `policy_priority_tag`가 회차마다 `None` ↔ `"elderly_benefits"`로
> 흔들리는 것임을 실측으로 확인했다. LLM이 `None`을 줘도 룰 기반으로 채워 anchor
> 검색이 항상 발동되도록 한다.
>
> **이번 패치 범위**: 룰 기반 fallback 한 함수 추가 + preprocessing 후처리 1줄 호출.
> 코드 변경 ~20줄. 회귀 위험 낮음(원래 동작은 LLM이 valid tag 줄 때와 동일).
>
> **작업 시간 예상**: 30~60분 (구현 15분, 단위 테스트 15분, 트레이스 검증 30분).

---

## 1. 배경 — 왜 이 작업이 필요한가

### 1.1 현상

운영자가 동일 질문을 반복해도 검색 결과 문서 셋이 회차마다 달라진다고 보고함.
예시: `"창원 노인 복지 추천해줘"` 를 같은 새 conv_id로 3회 호출하면:

| 회차 | `policy_priority_tag` | 응답 길이 | 문서 셋 |
|---:|---|---:|---|
| 1 | `elderly_benefits` | 1547자 | 셋 A |
| 2 | **`None`** | 1792자 | **셋 B (완전히 다름)** |
| 3 | `elderly_benefits` | 1547자 | 셋 A |

### 1.2 근본 원인

`UnifiedPreprocess` LLM이 같은 입력에 매번 다른 JSON을 뱉는다.
`seed=42`가 박혀 있음에도 발생 — vLLM 백엔드의 결정성 보장이 깨짐.
`response_generator.py` 호출 흐름 어디서도 막을 수 없는 LLM 백엔드 한계.

특히 `policy_priority_tag`가 `None`으로 떨어지는 회차에는:

1. `policy_extra_okms_searches(None, ...)` 가 `[]` 반환 → **anchor 추가 검색이 아예 안 호출됨**
2. `augment_okms_dual_query(None, ...)` 가 vector 부스트 안 함 → 임베딩 중심 이동 없음

→ 「기초연금」, 「노인맞춤돌봄」 같은 anchor가 가리키는 특정 사업 문서가
검색 풀에 들어오지 않아 결과 셋이 통째로 바뀐다.

### 1.3 실험으로 확인된 사실 (12회 트레이스)

| 조건 | 결과 |
|---|---|
| baseline `tag=elderly_benefits` | 셋 A, 일관 |
| baseline `tag=None` (자연 발생) | 셋 B (흔들렸던 케이스) |
| `tag=None` 강제 + 기본 cap | 셋 C, 일관 |
| `tag=None` 강제 + 수집 cap ×3 | **셋 C와 동일** — cap 늘리기 무용 |
| `tag=None` 강제 + 룰 기반 fallback | **셋 A 복원** — anchor 검색 복구 |

→ **수집 cap을 늘려도 anchor 없이는 결과가 회복되지 않음**.
→ **룰 기반 fallback이 anchor 검색을 강제로 발동시켜 baseline과 동일한 결과 셋을 만든다**.

---

## 2. 무엇을 만들 것인가

### 2.1 한 줄 요약

`UnifiedPreprocess`가 `policy_priority_tag=None`을 줄 때, **사용자 질문 키워드 매칭**으로
태그를 추론해서 채워준다(`elderly_benefits` / `implant` / `low_income` 중 하나).

### 2.2 동작 정의

```
입력: user_query 문자열
출력: "elderly_benefits" | "implant" | "low_income" | None

규칙:
- DB에 등록된 정책 키워드(`_get_tag_keywords_map()`)를 가져와 부분일치 검사
- 매칭된 태그가 하나면 그것을 반환
- 매칭된 태그가 둘 이상이면 우선순위(implant > low_income > elderly_benefits)로 1개 선택
  (이유: implant·low_income 이 elderly_benefits보다 더 좁은 특정성을 가짐)
- 매칭이 없으면 None 반환
```

### 2.3 적용 시점

`preprocessing.py` 의 `unified_preprocess` 함수 안, **LLM 응답을 파싱하고
DB excludes로 무력화한 직후**(line 329 다음).

```python
# 기존 코드 (line 310, 325-336 일부)
policy_priority_tag = _normalize_policy_priority_tag(_raw_policy_priority_tag_from_parsed(parsed))
...
try:
    from app.chat.infra.rag.policy_priority import strip_tag_by_exclusions
    before_tag = policy_priority_tag
    policy_priority_tag = strip_tag_by_exclusions(query, policy_priority_tag)
    ...
except Exception as e:
    logger.warning("[UnifiedPreprocess] exclude check failed: %s", e)

# [신규] excludes 적용 후에도 None이면 룰 기반 fallback
if policy_priority_tag is None:
    try:
        from app.chat.infra.rag.policy_priority import infer_policy_priority_tag_by_keywords
        inferred = infer_policy_priority_tag_by_keywords(query)
        if inferred:
            policy_priority_tag = inferred
            logger.info(
                "[UnifiedPreprocess] policy_priority_tag 룰 fallback: None → %s",
                inferred,
            )
    except Exception as e:
        logger.warning("[UnifiedPreprocess] tag inference fallback failed: %s", e)
```

**순서가 중요**: `strip_tag_by_exclusions`(LLM이 잘못 부여한 태그 무력화) 직후에 둔다.
exclusion으로 빼버린 태그를 룰이 다시 부활시키면 안 되므로 — 둘 다 통과한 query에 한해
적용. 단, 현재 exclusion 룰은 "치매" 같은 변별 키워드만 잡으므로 거의 모든 일반 질의는
이 fallback의 적용 대상이 된다.

---

## 3. 정확한 변경 지점

### 3.1 파일 A — `src/app/chat/infra/rag/policy_priority.py` (신규 함수 추가)

**위치**: 파일 끝부분 `infer_policy_priority_tag_by_keywords` 신규 함수 추가.
`_get_tag_keywords_map` 정의 뒤(같은 파일 안)면 어디든 OK.

```python
# 우선순위 — 더 좁은(specific) 태그가 앞.
# 노인 + 임플란트 동시 매칭 시 "임플란트 의료 지원"이 더 정밀한 anchor이므로 implant 우선.
_TAG_FALLBACK_PRIORITY: Tuple[str, ...] = ("implant", "low_income", "elderly_benefits")


def infer_policy_priority_tag_by_keywords(user_query: str) -> str | None:
    """LLM이 policy_priority_tag=None을 줄 때 호출되는 룰 기반 fallback.

    DB(gsnd_policy_priority)에 등록된 태그별 키워드를 가져와 user_query에 부분일치 검사.
    매칭된 태그가 하나면 그것, 둘 이상이면 _TAG_FALLBACK_PRIORITY 순서로 1개 선택.
    매칭 없으면 None.

    이 함수는 LLM 비결정성에 의해 같은 질문에 매번 다른 tag가 나오는 문제를 보정한다.
    """
    q = (user_query or "").strip()
    if not q:
        return None
    try:
        tag_keywords = _get_tag_keywords_map()
    except Exception as e:
        logger.warning("[PolicyBoost] fallback inference: keyword map load failed: %s", e)
        return None
    if not tag_keywords:
        return None

    matched: List[str] = []
    for tag in _TAG_FALLBACK_PRIORITY:
        keywords = tag_keywords.get(tag, ())
        if not keywords:
            continue
        if any(kw and kw in q for kw in keywords):
            matched.append(tag)

    if not matched:
        return None
    # 매칭이 여러 개여도 _TAG_FALLBACK_PRIORITY 순서로 첫 항목 반환
    return matched[0]
```

**중요**:
- `_get_tag_keywords_map()` 는 이 파일에 이미 정의되어 있고 DB 키워드를 캐시한다.
  새로 만들지 말 것.
- 하드코딩 키워드를 부활시키지 말 것 (`_DEFAULT_TAG_KEYWORDS`는 line 17-32에서
  의도적으로 빈 dict로 비활성화돼 있음 — DB only 정책).

### 3.2 파일 B — `src/app/chat/preprocessing.py` (호출 1줄 추가)

**위치**: `unified_preprocess` 함수 안, `strip_tag_by_exclusions` 호출 직후 (대략 line 336~337 사이).
정확한 위치는 다음 마커를 찾아서 그 **다음**에 삽입:

```python
except Exception as e:
    logger.warning("[UnifiedPreprocess] exclude check failed: %s", e)

# ← 이 자리에 아래 블록 삽입
```

삽입할 블록:

```python
# LLM이 None을 주거나 excludes로 무력화된 경우, 룰 기반 fallback으로 anchor 검색을
# 항상 발동시킨다. 같은 질문에 매 호출마다 다른 tag가 나와 검색 결과 셋이 흔들리는
# LLM 비결정성을 보정. 자세한 검증 데이터는 policy_priority_tag_fallback_handoff.md 참고.
if policy_priority_tag is None:
    try:
        from app.chat.infra.rag.policy_priority import infer_policy_priority_tag_by_keywords
        inferred = infer_policy_priority_tag_by_keywords(query)
        if inferred:
            policy_priority_tag = inferred
            logger.info(
                "[UnifiedPreprocess] policy_priority_tag 룰 fallback: None → %s",
                inferred,
            )
    except Exception as e:
        logger.warning("[UnifiedPreprocess] tag inference fallback failed: %s", e)
```

---

## 4. 단위 테스트

### 4.1 신규 테스트 파일

`tests/test_policy_priority_tag_fallback.py` 신규 작성. DB 의존을 피하기 위해
`_get_tag_keywords_map` 을 monkeypatch.

```python
"""policy_priority_tag 룰 기반 fallback 단위 테스트."""

import pytest

from app.chat.infra.rag import policy_priority as pp


@pytest.fixture
def stub_keywords(monkeypatch):
    """DB 호출을 피하고 알려진 키워드 맵을 주입."""
    fake_map = {
        "implant": ("임플란트", "치과", "구강", "틀니"),
        "low_income": ("저소득", "생계급여", "의료급여", "기초생활"),
        "elderly_benefits": ("기초연금", "노인맞춤돌봄", "어르신"),
    }
    monkeypatch.setattr(pp, "_get_tag_keywords_map", lambda: fake_map)
    return fake_map


def test_empty_query_returns_none(stub_keywords):
    assert pp.infer_policy_priority_tag_by_keywords("") is None
    assert pp.infer_policy_priority_tag_by_keywords(None) is None


def test_no_keyword_match_returns_none(stub_keywords):
    assert pp.infer_policy_priority_tag_by_keywords("양산 청년 일자리 알려줘") is None


def test_elderly_keyword_matches(stub_keywords):
    assert pp.infer_policy_priority_tag_by_keywords("창원 노인 복지 추천") is None  # "노인"은 키워드에 없음
    assert pp.infer_policy_priority_tag_by_keywords("기초연금 신청 알려줘") == "elderly_benefits"
    assert pp.infer_policy_priority_tag_by_keywords("어르신 돌봄 서비스") == "elderly_benefits"


def test_implant_keyword_matches(stub_keywords):
    assert pp.infer_policy_priority_tag_by_keywords("임플란트 의료 지원") == "implant"
    assert pp.infer_policy_priority_tag_by_keywords("치과 진료비 지원") == "implant"


def test_low_income_keyword_matches(stub_keywords):
    assert pp.infer_policy_priority_tag_by_keywords("저소득 의료비 지원") == "low_income"
    assert pp.infer_policy_priority_tag_by_keywords("기초생활수급자 혜택") == "low_income"


def test_priority_implant_over_elderly(stub_keywords):
    # "어르신 임플란트" → implant 우선 (더 좁은 anchor)
    assert pp.infer_policy_priority_tag_by_keywords("어르신 임플란트 지원") == "implant"


def test_priority_low_income_over_elderly(stub_keywords):
    # "저소득 어르신" → low_income 우선
    assert pp.infer_policy_priority_tag_by_keywords("저소득 어르신 지원") == "low_income"


def test_empty_keyword_map_returns_none(monkeypatch):
    monkeypatch.setattr(pp, "_get_tag_keywords_map", lambda: {})
    assert pp.infer_policy_priority_tag_by_keywords("임플란트") is None


def test_keyword_map_load_exception_returns_none(monkeypatch):
    def _raise():
        raise RuntimeError("db down")
    monkeypatch.setattr(pp, "_get_tag_keywords_map", _raise)
    assert pp.infer_policy_priority_tag_by_keywords("임플란트") is None
```

### 4.2 실행

```
cd C:/git/backend_2
venv/Scripts/python.exe -m pytest tests/test_policy_priority_tag_fallback.py -v
```

전부 통과해야 함.

---

## 5. 통합 검증 (E2E)

### 5.1 사전 준비

```
# .env에 RESPONSE_TRACE_ENABLED=True (또는 기동 시 env 주입)
$env:PYTHONPATH = 'src'
$env:RESPONSE_TRACE_ENABLED = 'True'
& 'C:/git/backend_2/venv/Scripts/python.exe' -m uvicorn app.main:app --host 127.0.0.1 --port 8003
```

JVM 초기화 30~60초 대기. `curl http://127.0.0.1:8003/docs` 가 200을 주면 ready.

### 5.2 검증 호출

Python 스크립트로 같은 질문 5회 호출(새 conv_id, single-turn):

```python
import json, time, urllib.request

Q = '창원 노인 복지 추천해줘'
for i in range(5):
    cid = f'verify-{int(time.time())}-{i}'
    body = json.dumps({
        'model': 'gsnd-rag',
        'messages': [{'role': 'user', 'content': Q}],
        'stream': False, 'user_id': cid, 'conv_id': cid,
    }, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        'http://127.0.0.1:8003/v1/chat/completions',
        data=body, headers={'Content-Type': 'application/json; charset=utf-8'}, method='POST',
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
        print(f'#{i} chars={len(content)}')
    time.sleep(1)
```

### 5.3 합격 기준

1. **trace JSONL 확인** (`log/response_trace/response_trace.jsonl` 최근 5개):
   - 모든 record의 `extras.policy_priority_tag` 가 `"elderly_benefits"` 여야 함
   - 단 한 건이라도 `None` 이면 fallback 미작동 — 호출 위치/순서 재점검

2. **서버 로그**:
   - LLM이 `None`을 줬을 때만 fallback 로그가 찍힘:
     `[UnifiedPreprocess] policy_priority_tag 룰 fallback: None → elderly_benefits`
   - LLM이 `elderly_benefits`를 줬을 때는 fallback 로그 없음

3. **문서 셋**:
   - 5회 모두 거의 동일한 문서 셋 (응답 길이 ±100자 이내)
   - LLM 응답 생성 단계의 미시 흔들림은 허용. 검색 결과 셋이 통째로 바뀌면 실패.

### 5.4 음성 검증 (negative test)

키워드가 안 매치되는 질문이 fallback에 의해 잘못 채워지지 않는지 확인:

```python
Q = '양산 청년 일자리 알려줘'  # implant/low_income/elderly 키워드 전무
# → trace의 policy_priority_tag는 LLM이 준 그대로(None 또는 LLM 판단값). fallback 룰 미작동.
```

---

## 6. 회귀 위험 / 금지 사항

### 6.1 깨지면 안 되는 것

- `strip_tag_by_exclusions` 가 무력화한 케이스: 룰이 다시 부활시키면 안 됨.
  → 현재 설계는 OK (`strip_tag_by_exclusions` 이후에 `None` 일 때만 룰 발동).
  → 단, exclusion에 의해 `None`이 된 직후 같은 query로 룰이 다시 같은 tag를 찍는
     edge case는 의도적이지 않을 수 있음. 발생 시 `strip_tag_by_exclusions` 가
     `False` flag도 같이 반환하도록 확장하는 후속 작업 필요(이번 패치 범위 밖).

- DB 키워드가 비어 있을 때(`_get_tag_keywords_map() == {}`): fallback이 가만히
  None 반환해야 함. 빈 dict로 KeyError 나면 안 됨. 위 구현은 이미 안전.

### 6.2 절대 하지 말 것

- **하드코딩 키워드 부활 금지** — `_DEFAULT_TAG_KEYWORDS` 가 `policy_priority.py:17-32`
  에서 의도적으로 빈 dict + 주석처리로 비활성화돼 있다. 이는 운영팀이 DB로 정책
  키워드를 관리하기 위한 결정이다. 같은 키워드를 룰 fallback에 하드코딩해서
  넣지 말 것. **DB(`_get_tag_keywords_map`) 만 사용**.

- **fallback을 `expanded_queries` / `reformed_query` 흔들림 보정에 확대 적용 금지**
  — 이번 패치는 `policy_priority_tag` 한 필드에만 집중. expanded_queries 흔들림은
  또 다른 변동 요인이지만 별도 작업(메모리 캐시 등)이 필요.

- **preprocessing 외 다른 위치에서 호출 금지** — 각 pipeline 진입부에 중복 호출하면
  로그가 중복되고 디버깅 복잡해짐. preprocessing 후처리에서 한 번만 채우고
  pipeline은 채워진 값을 받기만 함.

---

## 7. 커밋 가이드

### 7.1 브랜치

`patch/YYYYMMDD` 형식 (저장소 컨벤션). 예: `patch/20260526`.

### 7.2 커밋 분리

원자성을 위해 두 커밋 권장:

1. **커밋 1 — 함수 추가 + 단위 테스트**
   - `src/app/chat/infra/rag/policy_priority.py` (신규 함수)
   - `tests/test_policy_priority_tag_fallback.py` (신규)
   - 메시지: `policy_priority_tag 룰 기반 fallback 함수 추가 (호출처 없음)`

2. **커밋 2 — preprocessing 후처리에서 호출**
   - `src/app/chat/preprocessing.py` (1 블록 추가)
   - 메시지: `UnifiedPreprocess가 policy_priority_tag=None 줄 때 룰 fallback 적용`
   - 본문에 흔들림 데이터 요약 포함 (이 문서 1.1, 1.3 표 참고)

각 커밋 끝에 다음 trailer:
```
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

### 7.3 PR 본문

이 핸드오프 문서를 그대로 PR 본문에 붙여도 됨.
운영 적용 전 dev 환경에서 E2E 검증(§5.3 합격 기준) 한 번 통과 확인 후 머지.

---

## 8. 후속 작업 (이번 범위 밖)

이 패치는 **가장 큰 변동 요인 하나**만 잡는다. 남은 작은 변동 요인:

| 잔존 원인 | 영향 | 후속 작업 |
|---|---|---|
| `expanded_queries` LLM 변동 | 같은 tag 안에서도 또 갈림 (작음) | `(user_query, history_hash) → expanded_queries` 짧은 TTL in-memory 캐시 |
| `reformed_query` 미세 변동 | 검색 결과 거의 불변 | 위와 함께 처리 |
| Mariner 동점 정렬 | 순서만 영향, 개수 불변 | `WEIGHT desc, CHUNK_ID asc` 같이 안정 정렬 추가 |
| `UnifiedPreprocess` SLM ↔ 32B 폴백 | 잦지 않음 | SLM JSON 파싱 강화 또는 폴백 결정 로깅 |

본 패치 효과 측정 후 필요시 진행.

---

## 9. 참고 — 코드 위치 색인

| 항목 | 파일 | 라인 |
|---|---|---:|
| `process_rag_guide_recommend` 진입부 | `src/app/chat/infra/rag/pipeline_guide_recommend.py` | 58 |
| `unified_preprocess` 본체 | `src/app/chat/preprocessing.py` | ~200 |
| `policy_priority_tag` LLM 파싱 | `src/app/chat/preprocessing.py` | 310 |
| `strip_tag_by_exclusions` 호출 | `src/app/chat/preprocessing.py` | 329 |
| **fallback 삽입 자리** | `src/app/chat/preprocessing.py` | 337 직후 |
| `_get_tag_keywords_map` 정의 | `src/app/chat/infra/rag/policy_priority.py` | (파일 내 검색) |
| `_DEFAULT_TAG_KEYWORDS` (비활성) | `src/app/chat/infra/rag/policy_priority.py` | 17-32 |
| `POLICY_PRIORITY_TAGS` 상수 | `src/app/chat/infra/rag/policy_priority.py` | 529 |
| `policy_extra_okms_searches` (anchor 검색) | `src/app/chat/infra/rag/policy_priority.py` | 608 |
| `augment_okms_dual_query` (vector 부스트) | `src/app/chat/infra/rag/policy_priority.py` | 556 |
| 트레이스 dump | `src/app/chat/infra/rag/trace_sink.py` | — |
| 트레이스 ON 설정 | `src/app/core/config.py` | `RESPONSE_TRACE_ENABLED` |
