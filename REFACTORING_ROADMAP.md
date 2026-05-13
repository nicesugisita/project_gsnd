# 종합 리팩토링 로드맵

> 작성 기준일: 2026-05-13
> 분석 대상: `gsnd_rag_v4/backend` (FastAPI + Mariner5 + Q-RAG 기반 복지 챗봇)
> 분석 범위: 8축(intent·queryset·지역·정책·파이프라인·스트리밍·테스트·동시성) 중 코드 영역 6축

---

## 0. 목적과 원칙

### 0.1 리팩토링 목표
- **이식성** ↑ — 다른 지자체·도메인으로 재활용 가능한 구조
- **확장성** ↑ — 새 intent·컬렉션·응답 채널 추가 비용 절감
- **유지보수성** ↑ — 코드 중복 제거, 단일 진실 공급원(SSOT) 확립
- **운영 안전성** ↑ — 정책 변경을 코드 빌드 없이 적용

### 0.2 원칙
- **하드코딩 금지** — 매직 넘버·문자열·고유 명사를 코드에 박지 않음
- **점진적 마이그레이션** — 빅뱅 금지. 각 Phase는 단독으로 배포 가능해야 함
- **베스트 프랙티스·확장성** 우선 — YAGNI와 균형 (실 수요 없는 추상화 금지)
- **각 작업은 자체 테스트·검증** 동반 — 통과 시에만 다음 Phase 진입
- **레이어 책임 분리** — 도메인(business) / 인프라(I/O) / 어댑터(외부 시스템) 경계 명확화

### 0.3 본 로드맵에서 제외하는 것
- 비즈니스 요구사항 변경
- LLM 모델 교체·튜닝
- 인프라(쿠버네티스·DB 엔진) 변경
- 신규 기능 추가 (현 기능 유지하며 구조만 정리)

---

## 1. 현 상태 진단 요약

### 1.1 코드 규모
- 핵심 파이프라인 코드 약 **13,000줄**(`src/app/chat/**`, `src/app/mariner/queryset_*`)
- 가장 큰 파일: `_streaming.py` 787줄, `policy_priority.py` 781줄, `pipeline_utils.py` 715줄

### 1.2 주요 결합도 이슈 (분석에서 확인)

| 결합 영역 | 위치 (예시) | 영향 |
|---|---|---|
| 지역명 리터럴 ("경남"·"경상남도") | `extraction.py:107` `more_results.py:205` `pipeline_*.py 다수` | 타 지자체 이식 불가 |
| Queryset 6종 함수명·구조 제각각 | `mariner/queryset_*.py` | 컬렉션 추가 비용 ↑ |
| Intent 분기 코드 산재 | `_helpers.py:79` `_streaming.py:613,646` `router.py:365` | 새 intent 추가 ≥ 5군데 |
| 정책 규칙이 frozenset 하드코딩 | `policy_priority.py:697` | 정책 변경 시 코드 수정 |
| 스트리밍·비스트리밍 main flow 중복 | `_streaming.py` ↔ `router.py` | 신규 채널 추가 시 또 복제 |
| 파이프라인 파일 비대 | `pipeline_guide_recommend.py 670줄` 외 | 가독성·테스트성 ↓ |

### 1.3 이미 잘 되어 있는 부분 (유지)
- 도메인 디렉토리 계층화(`chat/document/conversation/tts/...`)
- 환경변수 기반 컬렉션 ID 분리(`Config.RAG_*_COLLECTION`)
- 프롬프트 외부화(`prompts/*.txt`)
- JPype 어댑터 격리

---

## 2. 우선순위 매트릭스

영향도(가로) × 난이도(세로)로 도식화:

```
                      낮은 난이도            높은 난이도
                ┌──────────────────────┬────────────────────────┐
   높은 영향도  │  P3: Intent 라우터    │  P1: 지역 격리         │
                │  P4: 정책 규칙 외부화 │  P2: Queryset 추상화   │
                ├──────────────────────┼────────────────────────┤
   낮은 영향도  │  P0: 자동화 토대 정비 │  P5: Pipeline 공통화   │
                │                       │  P6: 응답채널 통합     │
                └──────────────────────┴────────────────────────┘
```

권장 실행 순서: **P0 → P3 → P4 → P1 → P2 → P5 → P6**

- 빠른 승리(Quick Win): P3, P4 (낮은 난이도 + 즉시 효과)
- 전략적 자산: P1, P2 (큰 영향, 신중한 설계 필요)
- 기술 부채 해소: P5, P6 (장기)

---

## 3. Phase별 상세 계획

### P0. 자동화 토대 정비 (선행, 1~2일)

**목적**: 이후 모든 Phase의 안전망 마련.

**산출물**
- `tests/` 디렉토리 커버리지 측정 (`pytest --cov`) 베이스라인 보고서
- 핵심 흐름 회귀 시나리오 골든 케이스 5~10개 (사용자 질의 ↔ 기대 intent·파이프라인 매핑)
- 기존 `.claude/hooks/post_edit_verify.py`에 `.md`·`.txt` 프롬프트 무결성 검사 추가 (플레이스홀더 보존)
- CI에서 ruff·pytest 실행 게이트 (`.github/workflows` 또는 사내 CI)

**검증**
- 모든 회귀 시나리오가 현행 코드에서 통과
- 커버리지 베이스라인 수치 기록 → 이후 Phase에서 회귀 감지 기준

**의존**: 없음

---

### P3. Intent 라우팅 테이블화 (Quick Win, 2~3일)

**목적**: 새 intent 추가 시 수정 지점 1군데로 압축.

**핵심 변경**
- 신규 `chat/intent_registry.py`:
  ```python
  @dataclass(frozen=True)
  class IntentSpec:
      name: str
      processor: Callable          # 해당 RAG processor
      requires_lifecycle: bool = False
      supports_detail_form: bool = False
      reuse_history_on_more_info: bool = True
      # ... 메타데이터

  INTENT_REGISTRY: dict[str, IntentSpec] = {
      "general":         IntentSpec(...),
      "comparison":      IntentSpec(...),
      "guide_recommend": IntentSpec(...),
      "search":          IntentSpec(...),
  }
  ```
- `_helpers.py:_get_rag_processor` → `intent_registry.lookup(intent).processor`
- `_streaming.py`의 `if user_intent == "..."` 패턴을 `spec.requires_lifecycle` 등 메타데이터 조회로 치환
- `preprocessing.VALID_INTENTS` → registry에서 자동 생성

**검증**
- P0 회귀 시나리오 전수 통과
- 단위 테스트: `lookup("unknown")` 시 폴백 동작
- 라인 카운트: `_streaming.py` 30~50줄 감소

**리스크**
- 메타데이터 누락 시 silent failure → registry에 누락 검출 assertion 추가

---

### P4. 정책 규칙 외부화 (Quick Win, 1~2일)

**목적**: 정책·가중치·키워드 매핑을 코드에서 분리.

**핵심 변경**
- 신규 `config/policy_rules.yaml`:
  ```yaml
  priority_tags:
    implant:
      anchor_keywords: [...]
      exclude_patterns: [...]
    low_income: ...
    elderly_benefits: ...
  ```
- `policy_priority.py`의 `_basic_pen_cf` 같은 frozenset → YAML 로더로 교체
- `Config.POLICY_RULES_PATH` 설정 추가
- 캐시 갱신은 기존 `[PolicyBoost] cache refreshed` 패턴 유지(파일 mtime 감지)

**검증**
- YAML 로드 실패 시 기존 디폴트로 fallback
- 정책 매핑 비교 테스트 (마이그레이션 전후 결과 동일)
- 운영자가 YAML만 수정해도 재기동으로 반영

**리스크**
- YAML 문법 오류 → 시작 시 검증 + fail-fast

---

### P1. 지역(Region) 격리 (전략적 자산, 3~5일)

**목적**: "경남"·"경상남도" 리터럴을 코드에서 제거. 타 지자체 이식 가능 구조.

**핵심 변경**
- `Config` 신설 필드:
  ```
  REGION_FULL_NAME     = "경상남도"     # 정식명
  REGION_SHORT_NAMES   = ["경남"]       # 약칭(복수)
  REGION_OUT_OF_SCOPE_TEMPLATE = "..."  # 안내문 템플릿
  ```
- 신규 `chat/region.py`:
  ```python
  def is_in_region(text: str) -> bool: ...
  def extract_region_mentions(text: str) -> list[str]: ...
  def normalize_region_label(raw: str) -> str: ...
  REGION_PATTERN: re.Pattern  # 캐시된 동적 정규식
  ```
- 영향 파일 치환(분석에서 확인된 10여 곳):
  - `extraction.py`, `more_results.py`, `pipeline_general.py`, `pipeline_comparison.py`, `pipeline_guide_recommend.py`, `_streaming.py`, `_pipeline_steps.py`, `router.py`

**시군(시·군·구)은 별도 격리**: 이미 `sigun_utils.py`에 일부 정리됨. P1과 분리해 다음 Phase로 미룰 수 있음.

**검증**
- 회귀 시나리오 전수 통과
- 신규 가상 지역(`REGION_FULL_NAME=테스트도`)으로 더미 부팅 테스트
- `grep -rn "경남\|경상남도" src/app --include="*.py"` 결과 0건 (주석 제외)

**리스크**
- `sigun_utils.py`의 시군 데이터셋이 경남 종속 → P1에서는 손대지 않고 후속 단계로
- 정규식 캐싱 누락 시 성능 저하 → `functools.cache` 적용

---

### P2. Queryset 추상화 (전략적 자산, 5~8일)

**목적**: 6종 queryset 파일의 구조 통일·중복 제거. 새 컬렉션 추가 비용 절감.

**핵심 변경**
- 신규 `mariner/base.py`:
  ```python
  @dataclass(frozen=True)
  class CollectionSchema:
      collection_id: str
      select_fields: list[str]
      keyword_fields: list[FieldSpec]    # NAME_KO, TEXT_CHUNK_KO 등
      vector_fields:  list[FieldSpec]
      filter_fields:  dict[str, FilterSpec]   # SIGUN, COMPLI_DT 등
      identity_field: str                # CHUNK_ID / ID
      result_postprocess: Callable | None = None

  class BaseQueryset:
      def __init__(self, schema: CollectionSchema): ...
      async def query(self, keyword, *, filters, mode, ...) -> list[dict]: ...
  ```
- 기존 `queryset_*.py` → schema 정의 + 도메인 특이 후처리 함수만 유지
- 평균 400~500줄 → 100~150줄로 축소 예상
- 함수명 통일: `query_documents(collection_id, ...)`로 단일 진입점

**점진적 마이그레이션 순서**
1. `BaseQueryset` + `CollectionSchema` 도입 (기존 코드 영향 0)
2. 가장 단순한 `queryset_upload.py`부터 마이그레이션
3. `queryset_welfare.py`, `queryset_welfare_tel.py`, `queryset_gov_okms.py` 순서로
4. 마지막에 가장 복잡한 `queryset_gsnd.py`, `queryset_okms.py`
5. 각 단계 후 회귀 테스트 통과 시 다음으로

**검증**
- 마이그레이션 전후 검색 결과 동일성 (스냅샷 비교)
- 가중치·필터·Vector Search 옵션 누락 없는지 차이 검사 스크립트

**리스크**
- queryset_okms·gsnd의 후처리 로직이 복잡 → 도메인 특이 로직은 콜백으로 분리하고 schema 외부에 둠

---

### P5. Pipeline 공통 로직 추출 (장기, 5~7일)

**목적**: pipeline_*.py 4개의 시군 필터·중복 제거·정렬·LLM 호출 패턴을 `pipeline_utils.py`로 흡수.

**핵심 변경**
- `pipeline_utils.py` 확장 (현 715줄, 일부 정리 후 변동 가능):
  - `apply_sigun_filter(docs, sigun_filters)` — 공통화
  - `dedupe_by_chunk_id(docs)` — 공통화
  - `rerank_by_weight_and_date(docs)` — 공통화
  - `format_llm_input(docs, intent, template)` — 공통화
- 각 pipeline_*.py는 **도메인 특이 로직만** 남김:
  - `pipeline_general` — 보강 검색 분기
  - `pipeline_comparison` — 비교 속성·트리플 추출
  - `pipeline_guide_recommend` — 추천 로직·생애주기
  - `pipeline_search` — 시설 검색 분기

**검증**
- 회귀 시나리오 전수 통과
- pipeline_*.py 평균 라인 수 30% 이상 감소

**리스크**
- 공통화 과정에서 미묘한 도메인 차이 누락 → diff 비교 + 통합 테스트 필수

---

### P6. 응답 채널 추상화 (장기·선택, 4~6일)

**목적**: 스트리밍/비스트리밍 코드 중복 제거. 향후 음성·웹훅 등 채널 추가 기반.

**핵심 변경**
- 신규 `chat/response_channel.py`:
  ```python
  class ResponseChannel(Protocol):
      async def emit_status(self, msg: str) -> None: ...
      async def emit_token(self, token: str) -> None: ...
      async def emit_final(self, content: str, metadata: dict) -> None: ...

  class SSEChannel(ResponseChannel): ...
  class JSONChannel(ResponseChannel): ...
  ```
- `_streaming.py`와 `router.py`의 공통 흐름을 `chat/orchestrator.py`로 추출
- 본 흐름은 채널 의존성 없이 작성, 채널은 주입(DI)

**검증**
- 기존 SSE 응답이 바이트 단위로 동일
- 비스트리밍 JSON 응답도 형식 동일
- 신규 채널 PoC: 단순 stdout 채널 추가 → 5분 안에 동작 확인

**리스크**
- 단위 변경이 크고 광범위 → 회귀 위험 최대. P5 완료 후 시도 권장

---

## 4. 의존 관계 그래프

```
P0 ──┬─► P3 (라우터)         ─┐
     ├─► P4 (정책 외부화)     ─┤
     ├─► P1 (지역 격리)       ─┤
     │       │                 │
     │       ▼                 │
     ├─► P2 (Queryset)  ◄──────┘ (P1 결과 활용)
     │       │
     │       ▼
     └─► P5 (Pipeline 공통화) ◄── (P1, P2 결과 활용)
                │
                ▼
         P6 (응답 채널)
```

- P0는 모든 작업의 전제
- P1·P3·P4는 병렬 가능 (서로 독립)
- P2는 P1 완료 후가 유리 (지역 로직 제거된 상태에서 스키마 정의)
- P5는 P1·P2 결과를 활용
- P6은 가장 마지막

---

## 5. 일정 추정 (참고치)

| Phase | 작업일 (1인 기준) | 누적 |
|:---:|:---:|:---:|
| P0 | 1~2일 | 1~2 |
| P3 | 2~3일 | 3~5 |
| P4 | 1~2일 | 4~7 |
| P1 | 3~5일 | 7~12 |
| P2 | 5~8일 | 12~20 |
| P5 | 5~7일 | 17~27 |
| P6 | 4~6일 | 21~33 |

**총 21~33 작업일 (1인 풀타임 기준 약 5~7주)**

> ⚠️ 실측 데이터 없는 추정치. 실제 일정은 회귀 테스트 실행 시간·운영 부하·동시 진행 기능 작업 여부에 크게 좌우됨.

병렬화 가능 시 단축:
- P3·P4·P1을 2인이 분담 → 4~7일 → 2~3일로 단축

---

## 6. 마이그레이션 전략

### 6.1 점진적 원칙
- **각 Phase는 단독으로 배포 가능**해야 함 (배포 단위 = Phase 단위)
- 빅뱅 절대 금지
- 새 구조와 구 구조 **공존 기간** 허용 (예: P2에서 일부 컬렉션만 BaseQueryset 사용)

### 6.2 검증 게이트 (Phase 통과 조건)
- P0의 회귀 시나리오 전수 통과
- `pytest --cov` 커버리지가 P0 베이스라인 대비 동등 또는 상승
- `ruff check`·`ruff format` 통과
- 실 트래픽 샘플 100건 재현 시 응답이 의미적으로 동등 (LLM 응답이라 토씨 다를 수는 있음 — 의도·핵심 정보 기준)
- 검증 결과는 PR 본문에 표 형태로 첨부

### 6.3 롤백 전략
- 각 Phase는 독립 PR/브랜치로 진행
- 문제 발생 시 직전 커밋으로 revert
- Feature flag(`Config.USE_NEW_INTENT_REGISTRY` 등)로 신·구 코드 토글 가능하게 — 단, 정착되면 즉시 제거 (잔존 시 새로운 부채)

---

## 7. 리스크 및 완화

| 리스크 | 발생 가능성 | 영향 | 완화 |
|------|:---:|:---:|------|
| LLM 응답 변동으로 회귀 판정 어려움 | 高 | 中 | "의미 동등" 평가용 LLM-judge 도입, 골든 케이스는 intent·파이프라인 결정 수준에서만 비교 |
| Queryset 마이그레이션 중 검색 품질 저하 | 中 | 高 | 컬렉션별 순차 마이그레이션 + 스냅샷 비교 |
| 지역 격리 후 sigun 데이터 호환성 | 中 | 中 | P1은 지역 레벨만, 시군 격리는 후속 Phase로 분리 |
| 운영팀의 정책 YAML 작성 부담 | 中 | 低 | P4 산출물에 마이그레이션 스크립트·예제 YAML·검증 도구 포함 |
| 리팩토링 중 신규 기능 요구 발생 | 高 | 中 | Phase는 1~2주 단위로 짧게 유지, 사이사이 기능 작업 가능 |
| 테스트 환경에서 Mariner JVM 미가용 | 中 | 高 | 통합 테스트는 별도 마커(`pytest -m mariner`), 단위 테스트는 mock |

---

## 8. 성공 측정 지표 (KPI)

리팩토링 완료 후 정량 확인:

| 지표 | 현재(추정) | 목표 | 측정 방법 |
|------|:---:|:---:|------|
| 새 intent 추가 시 변경 파일 수 | 5~6개 | 1~2개 | 가상의 `feedback` intent 추가 시뮬레이션 |
| 새 컬렉션 추가 시 신규 코드 라인 | 400~500줄 | ≤ 150줄 | 더미 컬렉션 schema 작성 |
| 타 지자체 이식 시 변경 파일 수 | 10+ | 0 (Config만) | `Config.REGION_*` 변경만으로 부팅 |
| `_streaming.py` 라인 수 | 787 | < 500 | wc -l |
| pipeline_*.py 평균 라인 수 | 510 | < 400 | wc -l |
| 회귀 시나리오 통과율 | — | 100% | P0의 골든 케이스 |
| 코드 커버리지 | (베이스라인) | +5%p | `pytest --cov` |
| `grep "경남\|경상남도" src/app` | 10+ | 0 | grep |

---

## 9. 작업별 산출물 체크리스트

각 Phase 완료 시 다음을 PR에 첨부:

### 공통
- [ ] 변경 파일·라인 수 요약
- [ ] 회귀 시나리오 통과 결과
- [ ] 커버리지 차이
- [ ] 자체 검증(정적 분석·스키마 검증) 결과
- [ ] 롤백 절차 메모

### Phase별 특이 산출물
- **P0**: 회귀 시나리오 JSON, 커버리지 베이스라인 보고서
- **P3**: `intent_registry.py`, 새 intent 추가 가이드 문서
- **P4**: `policy_rules.yaml`, 마이그레이션 스크립트, 운영 매뉴얼
- **P1**: `region.py`, `Config.REGION_*` 항목 추가, 가상 지역 부팅 테스트 로그
- **P2**: `mariner/base.py`, 컬렉션별 schema 파일, 결과 동등성 검증 보고서
- **P5**: pipeline_*.py before/after 라인 수 비교
- **P6**: `response_channel.py`, 신규 채널 PoC

---

## 10. 단기 권장 행동 (Next 7 Days)

1. **이 로드맵에 대한 의사결정** — 진행 여부·우선순위 조정 (1일)
2. **P0 착수** — 회귀 시나리오 수집, 커버리지 베이스라인 측정 (1~2일)
3. **P3 또는 P4 중 Quick Win 1개 착수** — 가시적 성과로 모멘텀 확보 (2~3일)
4. **Phase별 PR 템플릿·체크리스트** 사내 합의 (0.5일)

---

## 11. 부록

### 11.1 참고 파일·라인 (분석 근거)
- 지역 결합: `extraction.py:107`, `more_results.py:205`, `pipeline_general.py:150`, `pipeline_comparison.py:164`, `pipeline_guide_recommend.py:115,649`, `_streaming.py:464`, `_pipeline_steps.py:86`
- Intent 분기: `_helpers.py:79~89`, `_streaming.py:301,507,613,646`, `router.py:365,379`, `preprocessing.py:21`
- 정책 하드코딩: `policy_priority.py:697,703,732`
- Queryset 진입점: `mariner/queryset_*.py` 각 파일 함수명

### 11.2 관련 스킬·메모리
- 스킬: `/mariner-jsp-api`, `/mariner-rest-api`, `/qrag-overview`, `/prompt-engineering`
- 메모리: `feedback_coding_guidelines.md` (하드코딩 금지), `feedback_self_verify.md` (자체 검증)

### 11.3 본 로드맵의 한계
- 동시성·캐싱·DB 부하 분석 미포함
- 보안·인증 영역 분석 미포함
- 실측 성능 데이터 부재 → 일정 추정은 참고치
- LLM 호출 비용·latency 측정 데이터 부재
