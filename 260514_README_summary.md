# 백엔드 핵심 요약

> 경상남도청 RAG 챗봇 백엔드 — 자세한 내용은 [`260514_README.md`](./260514_README.md) 참조.

---

## 1. 한 줄 요약

FastAPI · Python 3.12 · 사내 Mariner 벡터 검색 엔진(JPype/JVM) · 의도 분류 후 의도별 RAG 파이프라인 → LLM 응답 SSE 스트리밍.

## 2. 진입점

```
uvicorn app.main:app           # ← src/app/main.py
  └─ create_app()              # CORS + 라우터 7개 등록
       └─ lifespan (startup)   # JVM → LLM/DS httpx → DB 풀 → Retriever → 프롬프트 워밍업 → policy_priority 워커
```

**자원 공유 패턴**: httpx 커넥션 풀·DB 풀·MarinerRetriever 같은 무거운 자원은 startup 시 1회만 만들어 FastAPI 앱 인스턴스의 속성 `app.state`에 매달아 둡니다 (예: `app.state.llm_client = httpx.AsyncClient(...)`). 핸들러에서는 `request.app.state.llm_client` 로 다시 꺼내 재사용합니다.

> `app.state`는 디렉토리가 아니라 Starlette `FastAPI` 객체의 속성입니다 (`src/app/state/` 같은 폴더는 없음). 패키지명도 `app`, FastAPI 인스턴스 변수명도 `app`이라 헷갈리기 쉬우니 주의.

## 3. 도메인 지도

| 도메인 | 위치 | 주요 라우트 | 핵심 |
|---|---|---|---|
| chat | `src/app/chat/` | `/v1/chat/completions`, `/recommended-question`, `/recommend/collect`, `/suggest-questions` | **시스템 본체** — 8장 참조 |
| conversation | `src/app/conversation/` | `/v1/chat/conversations/*`, `/messages/feedback` | 대화 이력·피드백 CRUD |
| document | `src/app/document/` | `/v1/files/upload`, `/v1/documents/*` | 업로드 문서 QA·요약 |
| rag | `src/app/rag/` | `/v1/query/extract-triples`, `/expand`, `/re-query`, `/comparison-*` | DeepServer 프록시 |
| tts | `src/app/tts/` | `/api/tts/synthesize`, `/api/tts/stream`, WS `/api/stt/stream` | TTS·STT |
| keyword | `src/app/keyword/` | `/v1/keywords/extract` | kiwi 명사 추출 |
| system | `src/app/system/` | `/health` | 헬스체크 |

## 4. chat 도메인 안쪽

```
chat/
├── router.py            ← 진입점 (4개 엔드포인트)
├── _pipeline_steps.py   ← 단계 함수 (PreCheck/Sigun/Preprocess/Lifecycle)
├── _streaming.py        ← 스트리밍 흐름
├── _helpers.py          ← RAG 모드 분기
├── preprocessing.py     ← 통합 전처리 (LLM 1회 호출)
├── routing.py           ← 쿼리 재구성·확장·트리플·next_intent
├── more_results.py      ← 직전 preprocess/exclusion 히스토리 추출
├── intent_registry.py   ← 의도 라벨 단일 진실 공급원
└── infra/
    ├── llm/             ← LLM 클라이언트·판단 함수
    ├── deepserver/      ← DeepServer 클라이언트
    ├── stt/, db/        ← STT, 복지 DB
    └── rag/             ← ★ 의도별 파이프라인 (pipeline_*.py)
```

## 5. 의도와 후속 의도

| 1차 의도 | 파이프라인 |
|---|---|
| `general` | `pipeline_general.py` (기본) |
| `comparison` | `pipeline_comparison.py` (서비스 비교) |
| `guide_recommend` | `pipeline_guide_recommend.py` (생애주기·시군 맞춤) |
| `search` | `pipeline_search.py` (`search_target` 분기) |

| 후속 의도 (`classify_next_intent`) | 동작 |
|---|---|
| `MORE_INFO` | 직전 검색 재사용 + 노출 chunk/service 제외 → **guide_recommend 강제** |
| `MORE_DETAIL` | 특정 서비스 상세 (`re_query` 사용, 제외 X) |
| `NEW_SEARCH` / `REFINE_SEARCH` | 주제 전환 — 쿼리 재구성 스킵 |
| `CLARIFY_REPLY` | 되묻기 답변 — `query_recreation`에서 합성 |

## 6. 채팅 파이프라인 (8단계)

```
[1] Pre-Check         RAG 사용 여부 + 되묻기 감지
[2] 조기 시군 확인     되묻기 답변일 때 query_recreation 전 시군 확정
[3] 쿼리 재구성        멀티턴 문맥 반영 (MORE_INFO/NEW_SEARCH 시 스킵)
[4] 범위 체크          경남 외 지역 → 안내 메시지
[5] 시군 체크          실패 시 되묻기 (최대 3회)
[6] 통합 전처리        LLM 1회 → query/intent/reformed_query/expanded_queries
                       /search_target/policy_priority_tag (+ kiwi keywords)
[7] 생애주기 체크      guide_recommend 전용 로그
[8] RAG 실행           의도별 pipeline_*.py 호출
```

## 7. general RAG 

```
expanded_queries ─┐
keywords ─────────┼─→ OKMS Group A + GOV_OKMS + GSND **동시 병렬 검색**
필터 (sigun/year  │     (Mariner 호출은 동기 → run_in_executor)
 /lifecycle) ────┘
                  ↓
             top N (8건, more_detail이면 15건)
                  ↓
       (결과 < 임계치) → OKMS Fallback (lifecycle/anchor 제거)
                  ↓
             OKMS + GSND top 3 (more_detail 7) 합산
                  ↓
       정책 우선순위 부스팅 + LLM 관련성 필터 [8b/sllm]
                  ↓
       (0건) → Group A 하위 재시도 → Fallback 재검색
                  ↓
       상위 30% 최신순 / 나머지 가중치순 정렬
                  ↓
       generate_final_response_v2() [32b/luxia, SSE]
```

**구버전과의 차이**: OKMS 적합성 LLM 판단 제거, WELFARE_TEL 단계 제거, GSND를 항상 병렬 수집.

## 8. 반드시 알아야 할 함정

- **JVM 한 번 죽으면 못 살아남**: `_init_jvm()` 실패 시 Mariner 검색 전체 비활성. 로그 확인 필수.
- **Mariner는 동기**: 모든 호출은 `loop.run_in_executor()` 로 감쌀 것. 안 그러면 이벤트 루프 블로킹.
- **GSND는 시군 필터 미적용**: 광역 정책 문서의 SIGUN 메타가 비균일해서 통째로 누락되는 문제 회피용.
- **GSND `-60004` 타임아웃**: 1회 재시도(연도필터 해제), 누적 시 보강 검색 조기 중단.
- **MORE_INFO는 직전 intent 무시하고 guide_recommend로 강제**됨.
- **비로그인 사용자**: `user_id = conv_id`로 고정. DB 키는 `nologin{conv_id}`.
- **동시 사용자 50명 한도** (`MAX_CONCURRENT_USERS`, 세션 10분 만료).


## 9. 설정 (`.env`)

```bash
ENVIRONMENT=production           # development → DEBUG/LOG_LEVEL 자동 보정

# LLM
LLM_API_URL=...
LLM_API_TIMEOUT=120
NEXT_INTENT_LLM_ENABLED=true     # false면 후속 의도 항상 OTHER
QUERY_REWRITING_ENABLED=false    # true → unified_preprocessing_prompt_rewrite.txt 사용

```

전체 키 목록은 [`260514_README.md` 6장](./260514_README.md#6-설정-관리) 참조.

## 10. 어디부터 읽으면 되나

| 목적 | 시작 파일 |
|---|---|
| 채팅 한 턴 흐름 따라가기 | `src/app/chat/router.py` (`_chat_completions_core`) |
| 검색 단계 디버깅 | `src/app/chat/infra/rag/pipeline_general.py` |
| 의도 분류 / 전처리 수정 | `src/app/chat/preprocessing.py` + `prompts/unified_preprocessing_prompt.txt` |
| Mariner 쿼리 만지기 | `src/app/mariner/queryset_*.py` (먼저 `mariner5-api` skill 참고) |
| 프롬프트 수정 | `prompts/*.txt` (먼저 `prompt-engineering` skill 참고) |
| 새 엔드포인트 추가 | `src/app/{domain}/router.py` + `src/app/__init__.py` `register_routes()` |
| 설정 추가 | `src/app/core/config.py` `Settings` 클래스 |
