# LLM Eval Harness

YAML 기반 회귀 평가 하네스. `classify_next_intent`, `unified_preprocess` 같은
LLM 분류기/프롬프트의 입력→출력을 케이스로 정의하고, 실제 LLM을 호출해
매처(assertion)로 검증한다.

## 디렉토리

```
tests/llm_eval/
  schemas.py       # Case / CaseRun / SuiteResult 등 dataclass
  assertions.py    # equals, contains, intent_in, any_contains 등
  loader.py        # YAML → Case
  runner.py        # 케이스 실행 (N runs, pass rate)
  report.py        # 콘솔/JSON 리포트
  __main__.py      # CLI 진입
  runners/
    next_intent.py
    unified_preprocess.py
  cases/
    next_intent/
      guards.yaml
      regression.yaml
    unified_preprocess/
      context_preservation.yaml
```

## 실행

CLI (LLM 사내망 접속 가능한 환경 필요):

```bash
.venv/Scripts/python.exe -m tests.llm_eval                       # 전체
.venv/Scripts/python.exe -m tests.llm_eval --suite next_intent   # 경로 필터
.venv/Scripts/python.exe -m tests.llm_eval --tag guard_b         # 태그 필터
.venv/Scripts/python.exe -m tests.llm_eval --name application    # 이름 부분일치
.venv/Scripts/python.exe -m tests.llm_eval --json report.json    # JSON 리포트
```

pytest:

```bash
pytest -m llm_eval                                   # 평가 스위트만
pytest -m llm_eval -k guard_c                        # 부분일치
pytest                                               # 기본 실행에선 자동 제외
```

## 새 케이스 추가

`cases/<runner>/<suite>.yaml` 에 항목을 더한다.

```yaml
suite: next_intent · my_scenarios
runner: next_intent
defaults:
  is_clarification_question: false

cases:
  - name: my_new_case
    tags: [my_tag]
    runs: 3              # LLM 가변성이 우려되면 다회 실행
    pass_threshold: 0.66 # 3회 중 2회 이상 통과해야 PASS
    inputs:
      prior_intent: guide_recommend
      prior_service_names: ["A", "B"]
      user_query: "어떤 사업 알려줘"
      messages:
        - { role: user, content: "..." }
    asserts:
      - { op: equals, field: intent, value: MORE_INFO }
      - { op: contains, field: re_query, value: "추가" }
```

## 매처 목록

| op | 의미 |
|---|---|
| `equals` / `not_equals` | 값 일치/불일치 |
| `intent_in` / `intent_not_in` | 허용/금지 집합 |
| `contains` / `not_contains` | 부분 문자열 |
| `contains_all` | 모든 항목 부분 일치 |
| `matches_regex` | 정규식 검색 |
| `length_eq` / `length_gte` | 길이 조건 |
| `any_contains` / `all_contain` | 리스트의 각 원소에 대해 부분 일치 |
| `is_truthy` / `is_falsy` | 진리값 |

## 새 러너 추가

`runners/my_runner.py` 만들고 `register("my_runner", _run)` 호출. `cases/my_runner/`
디렉토리에 YAML을 두면 자동 발견.
