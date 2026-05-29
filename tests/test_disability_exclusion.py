"""장애인 전용 제도 하드 제외 필터 단위 테스트.

배경: 비장애 노인 질의("무릎 안좋아 거동 불편")에 장애인 전용 제도(장애인연금/보조기기/
활동보조 등)가 대거 노출되던 누수를 차단한다. 핵심은 '거동 불편/무릎'을 장애 단서로
오인하지 않으면서, 정당한 노인 대상 제도(예: 독거노인·장애인 응급안전안심)는 유지하는 것.
"""

from app.chat.infra.rag.pipeline_utils import (
    query_mentions_disability,
    exclude_disability_only_docs,
)


# ---------------------------------------------------------------------------
# query_mentions_disability
# ---------------------------------------------------------------------------

def test_disability_query_detected():
    assert query_mentions_disability("장애인 등록은 어떻게 하나요")
    assert query_mentions_disability("발달장애 아동 지원 알려줘")
    assert query_mentions_disability("뇌병변 환자 지원")
    assert query_mentions_disability("자폐 아이 돌봄")


def test_mobility_query_not_disability():
    # 이번 버그의 핵심: '거동 불편/무릎'은 장애 단서가 아니다.
    assert not query_mentions_disability(
        "엄마가 거제에 사는 70세 노인인데 무릎이 안좋아 거동이 불편해요"
    )
    assert not query_mentions_disability("거제 사는 노인 지원 뭐 있나요")


# ---------------------------------------------------------------------------
# exclude_disability_only_docs
# ---------------------------------------------------------------------------

def test_drop_disability_only_by_house_situation():
    docs = [
        {"CHUNK_ID": "1", "NAME": "기초연금", "HOUSE_SITUATION": "일반가구"},
        {"CHUNK_ID": "2", "NAME": "장애인연금", "HOUSE_SITUATION": "장애인"},
        {"CHUNK_ID": "3", "NAME": "노인일자리", "HOUSE_SITUATION": "일반가구,저소득"},
        {"CHUNK_ID": "4", "NAME": "장애인보조기기 지원", "HOUSE_SITUATION": "장애인"},
    ]
    kept = {d["CHUNK_ID"] for d in exclude_disability_only_docs(docs)}
    assert kept == {"1", "3"}


def test_keep_combined_general_and_disability():
    # HOUSE_SITUATION 에 '일반가구'가 함께 있으면 장애 병기라도 유지(광범위 대상).
    docs = [{"CHUNK_ID": "1", "NAME": "재가 돌봄", "HOUSE_SITUATION": "일반가구,장애인"}]
    assert len(exclude_disability_only_docs(docs)) == 1


def test_keep_other_specific_class():
    # 저소득/한부모 등 다른 특정계층은 장애 전용이 아니므로 유지.
    docs = [
        {"CHUNK_ID": "1", "NAME": "저소득 의료비", "HOUSE_SITUATION": "저소득"},
        {"CHUNK_ID": "2", "NAME": "한부모 양육비", "HOUSE_SITUATION": "한부모·조손"},
    ]
    assert len(exclude_disability_only_docs(docs)) == 2


def test_name_fallback_when_house_empty():
    # HOUSE_SITUATION 미색인 → 사업명에 '장애인'만 있으면 전용으로 보고 제외.
    docs = [
        {"CHUNK_ID": "1", "NAME": "장애인 도우미지원사업", "HOUSE_SITUATION": ""},
        {"CHUNK_ID": "2", "NAME": "어르신 무릎인공관절 수술 의료비 지원", "HOUSE_SITUATION": ""},
    ]
    kept = {d["CHUNK_ID"] for d in exclude_disability_only_docs(docs)}
    assert kept == {"2"}


def test_name_fallback_keeps_combined_population():
    # 사업명에 '장애인'이 있어도 '노인' 등 대상층이 함께면 전용으로 보지 않는다(보수적).
    docs = [
        {"CHUNK_ID": "1", "NAME": "독거노인·장애인 응급안전안심서비스", "HOUSE_SITUATION": ""},
    ]
    assert len(exclude_disability_only_docs(docs)) == 1


def test_bug_scenario_geoje_elderly():
    # 실제 버그 재현: 노인 질의 결과 10건 중 장애인 전용 6건이 섞임 → 4건만 남아야 함.
    docs = [
        {"CHUNK_ID": "1", "NAME": "어르신 무릎인공관절 수술 의료비 지원사업", "HOUSE_SITUATION": "일반가구"},
        {"CHUNK_ID": "2", "NAME": "기초연금", "HOUSE_SITUATION": "일반가구"},
        {"CHUNK_ID": "3", "NAME": "독거노인·장애인 응급안전안심서비스", "HOUSE_SITUATION": "일반가구"},
        {"CHUNK_ID": "4", "NAME": "장애인 도우미지원사업", "HOUSE_SITUATION": "장애인"},
        {"CHUNK_ID": "5", "NAME": "장애인보조기기 지원사업", "HOUSE_SITUATION": "장애인"},
        {"CHUNK_ID": "6", "NAME": "장애인연금", "HOUSE_SITUATION": "장애인"},
        {"CHUNK_ID": "7", "NAME": "장애인 건강검진기관 지원", "HOUSE_SITUATION": "장애인"},
        {"CHUNK_ID": "8", "NAME": "장애인자립자금대여", "HOUSE_SITUATION": "장애인"},
        {"CHUNK_ID": "9", "NAME": "장애인연금", "HOUSE_SITUATION": "장애인"},
        {"CHUNK_ID": "10", "NAME": "노인일자리 및 사회활동 지원사업", "HOUSE_SITUATION": "일반가구"},
    ]
    kept = {d["CHUNK_ID"] for d in exclude_disability_only_docs(docs)}
    assert kept == {"1", "2", "3", "10"}
