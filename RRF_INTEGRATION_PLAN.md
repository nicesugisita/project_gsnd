# RRF Reranker 검색 파이프라인 연결 Plan

> 전제: `src/app/chat/infra/rag/rrf_reranker.py`의 `rerank_by_rrf()` 인터페이스는 이미 존재(커밋 `fd50696`). 이 문서는 **파이프라인 연결**만 다룬다. 코드 편집은 별도 진행 신호 후 착수.

## 1. 목표

여러 검색 결과 리스트를 지금의 "이어붙인 뒤 WEIGHT 절대점수로 정렬" 방식 대신, **각 리스트 내 등수(rank) 기반 RRF 융합**으로 합친다. 출처가 다른 풀(예: center vs tel) 간 WEIGHT 스케일 차이로 인한 편향을 제거하는 것이 핵심 동기.

## 2. 현재 구조 — 병합 지점 인벤토리

전 파이프라인이 동일 관용구를 반복한다:

```python
sorted(_deduplicate_documents(A + B), key=lambda x: float(x.get("WEIGHT", 0) or 0), reverse=True)
```

| # | 파일:라인 | 병합 대상 | 융합 적합성 |
|---|---|---|---|
| 1 | `pipeline_search.py:236` | `center_docs + tel_docs` | ★ 높음 — 서로 다른 풀, 융합 의도 명확 |
| 2 | `pipeline_comparison.py:264` | `comp_group_a_docs + gov_okms_docs` | △ gov_okms 쿼터 정책 확인 필요 |
| 3 | `pipeline_comparison.py:312` | `okms_final + fb_a_docs` | △ fallback 보강 — 동질 소스 |
| 4 | `pipeline_comparison.py:320` | `okms_final` (단일) | ✗ 단일 리스트, 융합 불필요 |
| 5 | `pipeline_guide_recommend.py:273` | `gr_group_a_docs` (단일) | ✗ 단일 |
| 6 | `pipeline_guide_recommend.py:291` | `gov_okms_docs` (별도 쿼터) | ✗ **의도적 분리 풀** — 융합 금지 |
| 7 | `pipeline_guide_recommend.py:355/364` | `gr_top_docs + gr_fb_a_docs` | △ fallback 보강 |
| 8 | `pipeline_general.py:399` | `okms_group_a_docs + gov_okms_docs` | △ |
| 9 | `pipeline_general.py:450` | `okms_final + okms_fb_a_docs` | △ |
| 10 | `pipeline_general.py:473` | `okms_final + gsnd_top` | △ |

**중요한 관찰:** `center_docs`/`tel_docs`/`okms_group_a_docs` 등은 그 자체가 **여러 쿼리 결과를 `.extend()`로 평탄화한 concat**이다(`collect_welfare_center_tel_docs`, `collect_okms_groupa_and_gov_docs`). 즉 풀 하나가 "단일 정렬 리스트"가 아니다. RRF는 입력 리스트가 *각각 이미 정렬돼 있다*고 가정하므로, 이 전제를 어떻게 맞출지가 설계의 핵심.

## 3. 핵심 설계 결정

### D1. 입력 단위 (granularity)
- **(A) 풀 단위** — `rerank_by_rrf(sorted(center), sorted(tel))`. 각 풀을 WEIGHT로 먼저 정렬해 "정렬된 리스트" 전제를 충족시킨 뒤 2개 리스트로 융합. **변경 최소, 권장(Phase 1).**
- **(B) 쿼리 단위** — `collect_*`가 평탄화 대신 `List[List[doc]]`(쿼리별 결과)을 반환하도록 바꾸고 N개 리스트를 융합. RRF 취지에 가장 충실하나 `collect_*` 시그니처·호출부 전부 수정 필요. **Phase 2 검토.**
- 결정: **Phase 1은 (A)**. 효과 검증 후 (B) 확대 판단.

### D2. `_deduplicate_documents`와의 관계
- 기존 dedup은 CHUNK_ID 기준 **max-WEIGHT 인스턴스 유지**, RRF는 **first-seen 유지**.
- RRF의 기본 key도 CHUNK_ID라 식별 기준은 일치.
- 결정: **각 입력 리스트를 먼저 `_deduplicate_documents`로 풀 내 중복 제거 → RRF가 풀 간 동일 CHUNK_ID 융합**. (리스트 내 중복은 dedup이, 리스트 간 중복은 RRF가 담당. 역할 분리로 동작 명확.)

### D3. rrf_score vs WEIGHT (downstream 영향)
- RRF는 `rrf_score`만 채우고 WEIGHT는 건드리지 않는다. 그런데 **하류 단계가 WEIGHT를 계속 읽는다**: 관련성 필터, `apply_policy_priority_to_documents`, 8건 cap 정렬(`guide_recommend.py:649`), 로깅.
- 위험: 융합 직후 누군가 다시 `sorted(key=WEIGHT)` 하면 RRF 순서가 무너진다.
- 결정: **융합 결과는 RRF 순서를 그대로 보존**하고, 그 이후 단계에서 WEIGHT 재정렬이 없는지 호출 경로별로 확인(연결 작업 체크리스트). 8건 cap 등 "상위 N" 절단은 WEIGHT 대신 RRF 순서(이미 정렬됨)의 앞 N개로 교체.

### D4. 가중치 (weights)
- center vs tel 등 풀별 중요도. **Phase 1은 균등(1.0)** 기본. Config로 조정 가능하게 열어둠.

### D5. top_k
- search는 현재 cap 없이 전부 반환(시설/연락처 누락 방지). **`top_k=0`(전체) 유지.**

### D6. 피처 플래그 (안전 롤아웃)
- `bec4812`의 `VS_THRESHOLD`처럼 Config 플래그 추가: `RRF_FUSION_ENABLED`(기본 `False`).
- `True`면 RRF 융합, `False`면 기존 WEIGHT 정렬. A/B 및 즉시 롤백 경로 확보.

## 4. 단계별 작업

### Phase 0 — 준비
- [ ] `Config`에 `RRF_FUSION_ENABLED`(bool, 기본 False), 선택적 `RRF_WEIGHTS_CENTER_TEL` 추가.
- [ ] `pipeline_search.py`에 `from app.chat.infra.rag.rrf_reranker import rerank_by_rrf` import.

### Phase 1 — pipeline_search 풀 융합 (pilot, 병합지점 #1)
- [ ] `pipeline_search.py:236` 교체: 플래그 분기.
  - ON: 각 풀 dedup→정렬 후 `rerank_by_rrf(center_sorted, tel_sorted, weights=...)`.
  - OFF: 기존 로직 그대로.
- [ ] 단일 풀 모드(`_tel_only`/`_center_only`)는 융합 대상 아님 → 기존 경로 유지.
- [ ] Step5 이후 WEIGHT 재정렬 없는지 확인(현재 코드상 없음, 관련성 필터로 직행).
- [ ] 검증(§6) 후 결과 비교 → 효과 판단.

### Phase 2 — 확대 (Phase 1 효과 확인 후)
- [ ] 적합성 △ 지점(#2,#3,#7,#8~#10) 개별 검토. **단일 리스트(#4,#5)와 의도적 분리 쿼터(#6)는 제외.**
- [ ] 동질 소스 fallback 보강(#3,#7,#9)은 융합 이득이 작을 수 있음 — 측정 후 결정.
- [ ] (선택) D1-(B) 쿼리 단위 융합으로 `collect_*` 리팩터 검토.

## 5. Phase 1 코드 스케치 (참고용, 미적용)

```python
# pipeline_search.py — 양쪽 풀 경로
else:
    if Config.RRF_FUSION_ENABLED:
        center_sorted = sorted(_deduplicate_documents(center_docs),
                               key=lambda x: float(x.get("WEIGHT", 0) or 0), reverse=True)
        tel_sorted = sorted(_deduplicate_documents(tel_docs),
                            key=lambda x: float(x.get("WEIGHT", 0) or 0), reverse=True)
        top_docs = rerank_by_rrf(center_sorted, tel_sorted)  # rrf_score 채워짐, 정렬 완료
        logger.info("[RAG/search_v2] RRF 융합: center=%d tel=%d → %d",
                    len(center_sorted), len(tel_sorted), len(top_docs))
    else:
        all_docs = center_docs + tel_docs
        top_docs = sorted(_deduplicate_documents(all_docs),
                          key=lambda x: float(x.get("WEIGHT", 0) or 0), reverse=True)
```

> RRF ON 경로에서는 `all_docs` 변수 의존 로그(`:241`)를 조정 필요.

## 6. 테스트 계획 (메모리: 수정 후 직접 실행·검증 필수)

- [ ] **단위:** `tests/test_rrf_reranker.py` 기존 15 시나리오 통과 확인(인터페이스 무변경 회귀).
- [ ] **통합:** 멀티턴 하니스(`tests/run_gsnd_total_v02_multiturn.py` 등)로 플래그 ON/OFF 양쪽 실행.
- [ ] **A/B 비교:** 동일 질의셋에 대해 OFF vs ON top_docs 순서·구성 diff. 시설/연락처 누락 0 확인(search 특성).
- [ ] **로그 검증:** `[RRF] k=60 ...` debug 로그로 in/out 건수·점수 범위 sanity check.

## 7. 리스크 / 주의

- **R1 (downstream WEIGHT 재정렬):** RRF 순서가 이후 단계에서 WEIGHT 재정렬로 덮이면 무의미. 경로별 확인 필수(D3).
- **R2 (쿼터 풀 오염):** guide_recommend의 gov_okms는 의도적 분리 쿼터(#6). 융합 금지.
- **R3 (dedup 인스턴스 차이):** max-WEIGHT vs first-seen. D2 순서(dedup 먼저)로 회피.
- **R4 (정렬 전제):** 풀이 정렬돼 있지 않으면 RRF rank가 무의미 → Phase 1에서 명시적 사전 정렬로 보장.
- **R5 (CHUNK_ID 없는 문서):** `_default_key`가 `id(doc)` 폴백 → 융합 안 되고 개별 취급. 빈도·영향 확인.

## 8. 롤백

- `RRF_FUSION_ENABLED=False`로 즉시 기존 동작 복귀. 코드 제거 불필요.

---
*작성일: 2026-05-24 · 상태: 검토 대기(구현 미착수)*
