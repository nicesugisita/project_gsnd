"""생애주기 멀티태그 파이프라인 단위 테스트.

대상:
- preprocessing._normalize_lifecycle_tags   (7라벨 화이트리스트·중복·빈배열)
- queryset_gov_okms._expand_lifecycle_for_gov_okms  (노인↔노년 양방향 유의어)
- pipeline_utils.filter_gov_okms_docs_by_lifecycle  (멀티태그 any-of·유의어·str호환)
- pipeline_utils.prioritize_general_household        (멀티태그 any-of + min_keep 백필)

서버/JVM/Mariner 불필요(순수 함수). LLM이 실제로 태그를 산출하는지·Mariner 소프트
부스트 동작은 별도 E2E 실측 필요(이 테스트 범위 밖).
"""

from app.chat.preprocessing import (
    _normalize_lifecycle_tags,
    _normalize_household_tags,
    _normalize_policy_tags_to_single,
)
from app.mariner.queryset_gov_okms import _expand_lifecycle_for_gov_okms
from app.chat.infra.rag.pipeline_utils import (
    filter_gov_okms_docs_by_lifecycle,
    prioritize_general_household,
)


# --- _normalize_lifecycle_tags ---

def test_normalize_whitelist_and_dedup():
    assert _normalize_lifecycle_tags({"lifecycle_tags": ["노인", "아동", "위기", "노인"]}) == ["노인", "아동"]


def test_normalize_empty_and_absent():
    assert _normalize_lifecycle_tags({"lifecycle_tags": []}) == []
    assert _normalize_lifecycle_tags({}) == []


def test_normalize_pregnancy_label_and_camelcase():
    assert _normalize_lifecycle_tags({"lifecycle_tags": ["임신·출산"]}) == ["임신·출산"]
    assert _normalize_lifecycle_tags({"lifecycleTags": ["청년"]}) == ["청년"]


# --- _expand_lifecycle_for_gov_okms (유의어 양방향) ---

def test_expand_elderly_synonyms_bidirectional():
    assert set(_expand_lifecycle_for_gov_okms("노인")) == {"노인", "노년"}
    assert set(_expand_lifecycle_for_gov_okms("노년")) == {"노년", "노인"}


def test_expand_passthrough_and_empty():
    assert _expand_lifecycle_for_gov_okms("아동") == ["아동"]
    assert _expand_lifecycle_for_gov_okms("") == []
    assert _expand_lifecycle_for_gov_okms(None) == []


# --- filter_gov_okms_docs_by_lifecycle (멀티태그 any-of) ---

def _docs():
    return [
        {"CHUNK_ID": "1", "LIFE_CYCLE": "노년", "WEIGHT": 0.9},
        {"CHUNK_ID": "2", "LIFE_CYCLE": "노인", "WEIGHT": 0.8},
        {"CHUNK_ID": "3", "LIFE_CYCLE": "아동", "WEIGHT": 0.7},
        {"CHUNK_ID": "4", "LIFE_CYCLE": "", "WEIGHT": 0.6},  # 빈필드 보수적 유지
    ]


def test_filter_synonym_keeps_both_and_empty():
    kept = {d["CHUNK_ID"] for d in filter_gov_okms_docs_by_lifecycle(_docs(), ["노인"])}
    assert kept == {"1", "2", "4"}  # 노년/노인 유의어 + 빈필드 유지, 아동 제거


def test_filter_empty_tags_passthrough():
    assert len(filter_gov_okms_docs_by_lifecycle(_docs(), [])) == 4
    assert len(filter_gov_okms_docs_by_lifecycle(_docs(), None)) == 4


def test_filter_str_compat():
    assert len(filter_gov_okms_docs_by_lifecycle(_docs(), "노인")) == 3


def test_filter_multitag_union():
    kept = {d["CHUNK_ID"] for d in filter_gov_okms_docs_by_lifecycle(_docs(), ["노인", "아동"])}
    assert kept == {"1", "2", "3", "4"}


# --- prioritize_general_household (멀티태그 any-of + 백필) ---

def _pool():
    return [
        {"CHUNK_ID": "1", "LIFE_CYCLE": "노년", "WEIGHT": 0.9, "HOUSE_SITUATION": "일반가구"},
        {"CHUNK_ID": "2", "LIFE_CYCLE": "노인", "WEIGHT": 0.8, "HOUSE_SITUATION": "일반가구"},
        {"CHUNK_ID": "3", "LIFE_CYCLE": "아동", "WEIGHT": 0.7, "HOUSE_SITUATION": "일반가구"},
    ]


def test_pgh_lifecycle_anyof_synonym():
    res = prioritize_general_household(_pool(), target=8, min_keep=1, require_general=False, lifecycle=["노인"])
    ids = {d["CHUNK_ID"] for d in res}
    assert "3" not in ids       # 아동 부적합
    assert {"1", "2"} <= ids    # 노년/노인 유의어 적합


def test_pgh_min_keep_backfill():
    # 적합 0건이어도 min_keep 만큼 WEIGHT 상위로 백필 (3~4건 붕괴 방지)
    res = prioritize_general_household(_pool(), target=8, min_keep=2, require_general=False, lifecycle=["청소년"])
    assert len(res) == 2


def test_pgh_backfill_to_target_demotes_not_drops():
    # 완화 모드(LLM 선별): 태그 불일치 부적합도 drop 하지 않고 적합 뒤로 밀어 target 까지 유지.
    # 노인 질의 → 적합 {1,2}, 부적합 {3}. target=3 이면 셋 다 유지(3 은 맨 뒤).
    res = prioritize_general_household(
        _pool(), target=3, min_keep=1, require_general=False,
        lifecycle=["노인"], backfill_to_target=True,
    )
    ids = [d["CHUNK_ID"] for d in res]
    assert ids == ["1", "2", "3"]   # 적합 우선(WEIGHT 순) + 부적합 demote


def test_pgh_backfill_to_target_keeps_fit_priority_within_cap():
    # target 이 풀보다 작으면 적합이 cap 을 우선 차지하고 부적합은 잘린다(적합 우선 보존).
    res = prioritize_general_household(
        _pool(), target=2, min_keep=1, require_general=False,
        lifecycle=["노인"], backfill_to_target=True,
    )
    ids = {d["CHUNK_ID"] for d in res}
    assert ids == {"1", "2"}        # 적합 2건이 cap 채움, 부적합 3 은 자연 탈락


def test_pgh_default_still_hard_drops():
    # 기본 모드(backfill_to_target=False)는 기존대로 부적합 하드드롭 유지.
    res = prioritize_general_household(_pool(), target=8, min_keep=1, require_general=False, lifecycle=["노인"])
    ids = {d["CHUNK_ID"] for d in res}
    assert "3" not in ids


# --- _normalize_household_tags (가구상황 7종 화이트리스트) ---

def test_household_whitelist_and_dedup():
    assert _normalize_household_tags({"household_tags": ["저소득", "다자녀", "위기", "저소득"]}) == ["저소득", "다자녀"]


def test_household_general_when_no_specific():
    assert _normalize_household_tags({"household_tags": ["일반가구"]}) == ["일반가구"]


def test_household_specific_drops_general():
    # 특정계층이 있으면 '일반가구'는 제거(특정계층 우선)
    assert _normalize_household_tags({"household_tags": ["저소득", "일반가구"]}) == ["저소득"]


def test_household_bohun_and_invalid_and_empty():
    assert _normalize_household_tags({"household_tags": ["보훈"]}) == ["보훈"]
    assert _normalize_household_tags({"household_tags": ["없는값"]}) == []
    assert _normalize_household_tags({}) == []


# --- _normalize_policy_tags_to_single (정책 단일·우선순위) ---

def test_policy_single_and_priority():
    assert _normalize_policy_tags_to_single({"policy_tags": ["elderly_benefits"]}) == "elderly_benefits"
    assert _normalize_policy_tags_to_single({"policy_tags": ["implant", "elderly_benefits"]}) == "implant"
    assert _normalize_policy_tags_to_single({"policy_tags": ["low_income", "elderly_benefits"]}) == "low_income"


def test_policy_empty_and_invalid():
    assert _normalize_policy_tags_to_single({"policy_tags": []}) is None
    assert _normalize_policy_tags_to_single({"policy_tags": ["없는값"]}) is None
    assert _normalize_policy_tags_to_single({}) is None
