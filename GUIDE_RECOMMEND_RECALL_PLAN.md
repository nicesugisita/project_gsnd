# guide_recommend OKMS recall 개선 Plan (rewrite 모드 유지)

> 전제: B1(필터→cap)으로 후보가 충분하면 8건을 채우지만, sparse 쿼리(예: "창원 기초생활수급자")는 OKMS 유니크 후보가 5건뿐이라 8 미달(7). 원인은 컬렉션 부족이 아니라 **rewrite 모드의 단일 쿼리로 인한 검색 다양성 부족**(진단 완료). 이 문서는 설계만; 코드는 진행 신호 후 착수.

## 1. 검증된 근거

같은 질의("창원 기초생활수급자 혜택"), 쿼리 모드만 변경:

| | rewrite (단일, 현재) | 확장 (다중, 테스트) |
|---|---|---|
| 생성 쿼리 수 | 1 | 3 |
| OKMS 수집 → dedup | 10 → **5** | 20 → **7** |
| 필터 후 OKMS 생존 | 3 | **5** |
| 최종 추천 | **7** | **8** ✅ |

→ 컬렉션에 창원 기초수급 서비스는 5개보다 많다. **단일 rewrite 쿼리(벡터+트리플이 같은 5개 반환)가 못 끌어올 뿐.**

## 2. 설계 원칙

- **전역 `QUERY_REWRITING_ENABLED=True` 유지** — rewrite 모드는 정밀도↑·Mariner 호출↓ 의 의도적 선택(search/general 등에 유효). 전역 토글로 끄지 않는다.
- **recall 개선은 guide_recommend(추천 의도)에만 스코프** — 추천은 "서비스 8개"라 breadth 가 본질. 다른 의도는 무영향.
- **"무조건 8"이 아니다** — 컬렉션에 진짜 relevant 가 8 미만인 쿼리는 그대로 <8 반환(옳음). 목표는 *relevant 가 있는데 단일 쿼리가 놓치는* 케이스 제거.

## 3. 근본 위치 (코드)

- `preprocessing.py:268-271` — rewrite 모드면 `expanded = [reformed]` 단일로 **강제**(LLM 확장쿼리 폐기).
- `pipeline_guide_recommend.py:155-175` — `precomputed_expanded_queries`(rewrite 모드=단일)가 있으면 그대로 쓰고, **확장 함수 `expand_query()`(169행)는 건너뜀**.

즉 다중쿼리 생성 머신(`expand_query`)은 이미 있고, recommend 에서 호출되지 않을 뿐이다.

## 4. 방안 비교

| 방안 | 내용 | 스코프 | 평가 |
|---|---|---|---|
| **R2 (권장)** | guide_recommend 에서 rewrite 모드여도 `expand_query(gr_expand_base)` 로 다중 확장쿼리를 생성해 OKMS 검색에 사용 (단일 reformed + 확장쿼리 병합) | guide_recommend 국소 | 기존 `expand_query` 재활용, 다른 의도 무영향, 변경 최소 |
| R1 | preprocessing 에서 `intent==guide_recommend` 면 rewrite 모드에서도 expanded 유지/생성 | 전처리 중앙 | rewrite 프롬프트가 expanded 도 출력하게 변경 동반 — 영향범위 큼 |
| R3 (보조) | GOV `max_results`↑, dedup 완화 등 | 부차 | 진단상 OKMS 다양성이 핵심이라 효과 제한적 |

## 5. 권장안 R2 상세

**guide_recommend 의 초기 OKMS 검색에 다중 확장쿼리를 공급한다(rewrite 모드와 무관).**

- `pipeline_guide_recommend.py:155-175` 의 분기 수정:
  - rewrite 모드여도 recommend 의도에서는 `expand_query(gr_expand_base)` 를 호출해 N개 확장쿼리 확보.
  - precomputed 단일 reformed + expand 결과를 `dedupe_cap_expanded_queries` 로 병합·중복제거.
- 이후 기존 GroupA 검색이 다중 쿼리로 돌아 유니크 OKMS 후보가 늘어남 → B1 의 "필터→cap" 과 결합해 8건 충족 확률↑.
- **재귀 보강(D-1.5)은 이미 `precomputed_expanded_queries or gr_expanded` 를 쓰므로**, 초기 단계만 broaden 하면 일관됨.

### 왜 R2 인가
- `expand_query` 가 이미 존재(169행 fallback) → 신규 머신 불필요.
- 변경이 guide_recommend 한 함수에 갇힘 → search/general/comparison 무영향.
- 전역 rewrite 정밀도 정책 보존.

## 6. 엣지 / 트레이드오프

- **지연시간**: `expand_query` LLM 호출 1회 + Mariner 쿼리 수 증가. 추천 의도는 이미 재귀 보강으로 다중 쿼리를 감수하므로 허용 범위로 추정 — **측정 필요**.
- **정밀도**: 확장쿼리는 rewrite 단일쿼리보다 정밀도가 낮을 수 있으나, **B1 의 관련성 필터가 후단에서 노이즈를 거른다**(filter→cap 구조라 안전).
- **진짜 sparse 쿼리**: 확장해도 컬렉션에 8개 미만이면 그대로 <8(정상).
- **dedup**: 확장쿼리 간 중복은 기존 `_deduplicate_documents`/`dedupe_cap_expanded_queries` 가 처리.

## 7. 테스트 계획 (메모리: 수정 후 직접 실행·검증)

- sparse 쿼리("창원 기초생활수급자" 등)로 **rewrite 모드 유지한 채** 8건 도달 확인.
- 다른 의도(search/general/comparison) **무영향** 확인.
- **지연시간 측정** — expand 추가에 따른 응답시간 증가폭 확인(추천 의도 한정).
- 관련성 필터가 여전히 노이즈를 거르는지(품질 회귀 없음) 확인.

## 8. 롤백
변경은 guide_recommend 초기 쿼리 생성 분기에 국한 — 되돌리기 쉬움. 전역 토글·프롬프트 미변경.

---
*작성일: 2026-05-24 · 상태: 검토 대기(구현 미착수) · 선행: B1 커밋(9c12a27), project_guide_recommend_8svc 메모리*
