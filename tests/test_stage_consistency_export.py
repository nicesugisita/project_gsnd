"""export_stage_consistency 익스포터 로직 단위 테스트 (서버 불필요).

같은 질문 3회 트레이스 중 policy_tag 가 1회 흔들리는 합성 데이터로,
diff 시트가 흔들리는 단계만 골라내는지 검증한다.
"""

from tests.export_stage_consistency import assign_rounds, build_workbook


def _rec(ts, q, *, intent="guide_recommend", reformed="노인 복지", tag="elderly_benefits",
         search_where="( BUSINESS_NAME_KO[HASANY]=\"노인 복지\" )", docs=None, resp="응답A"):
    return {
        "ts": ts,
        "trace_id": ts,
        "intent": intent,
        "user_question": q,
        "top_docs": docs if docs is not None else [{"BUSINESS_NAME": "기초연금"}],
        "response_text": resp,
        "extras": {
            "intent": intent,
            "reformed_query": reformed,
            "expanded_queries": ["노인 복지", "어르신 지원"],
            "keywords": ["노인", "복지"],
            "policy_priority_tag": tag,
            "vector_query": reformed,
            "search_queries": [
                {"label": "OKMS#0", "leg": "keyword", "query": "노인 복지",
                 "filters": [{"field": "SIGUN", "value": "경상남도 창원시"}], "where": search_where},
            ],
        },
    }


def test_assign_rounds_orders_by_ts():
    q = "창원 노인 복지 추천해줘"
    recs = [_rec("2026-05-26T10:00:03", q), _rec("2026-05-26T10:00:01", q), _rec("2026-05-26T10:00:02", q)]
    out = assign_rounds(recs)
    rounds = {r["ts"]: r["_round"] for r in out}
    assert rounds["2026-05-26T10:00:01"] == 1
    assert rounds["2026-05-26T10:00:02"] == 2
    assert rounds["2026-05-26T10:00:03"] == 3


def test_diff_flags_flapping_policy_tag():
    q = "창원 노인 복지 추천해줘"
    recs = [
        _rec("2026-05-26T10:00:01", q, tag="elderly_benefits", docs=[{"BUSINESS_NAME": "기초연금"}]),
        # 2회차: 태그가 None 으로 흔들리며 문서 셋도 바뀜
        _rec("2026-05-26T10:00:02", q, tag="None", docs=[{"BUSINESS_NAME": "다른제도"}]),
        _rec("2026-05-26T10:00:03", q, tag="elderly_benefits", docs=[{"BUSINESS_NAME": "기초연금"}]),
    ]
    recs = assign_rounds(recs)
    wb = build_workbook(recs)

    assert "stages" in wb.sheetnames
    assert "diff" in wb.sheetnames

    wd = wb["diff"]
    headers = [c.value for c in wd[1]]
    # 데이터 행 1개 (질문 1종)
    row = [c.value for c in wd[2]]
    cells = dict(zip(headers, row))

    # 흔들린 단계: 정책태그 / 검색문서 → ⚠흔들림
    assert "⚠흔들림" in cells["정책태그(policy_tag)"]
    assert "⚠흔들림" in cells["검색문서(docs)"]
    # 안 흔들린 단계: 의도분류 / 쿼리재구성 → ✓고정
    assert "✓고정" in cells["의도분류(intent)"]
    assert "✓고정" in cells["쿼리재구성(reformed)"]


def test_detail_sheet_shows_flap_values_per_round():
    q = "창원 노인 복지 추천해줘"
    recs = assign_rounds([
        _rec("2026-05-26T10:00:01", q, tag="elderly_benefits"),
        _rec("2026-05-26T10:00:02", q, tag="None"),
        _rec("2026-05-26T10:00:03", q, tag="elderly_benefits"),
    ])
    wb = build_workbook(recs)
    assert "변동상세" in wb.sheetnames
    wf = wb["변동상세"]
    rows = list(wf.iter_rows(values_only=True))
    hdr = rows[0]
    assert hdr[:3] == ("질문", "단계", "값종류수")
    assert "R1" in hdr and "R3" in hdr
    # 정책태그 흔들림 행: R1=elderly, R2=None, R3=elderly 가 나란히
    tag_rows = [r for r in rows[1:] if r[1] == "정책태그(policy_tag)"]
    assert len(tag_rows) == 1
    r = tag_rows[0]
    assert r[2] == 2  # 값종류수
    assert r[3] == "elderly_benefits" and r[4] == "None" and r[5] == "elderly_benefits"
    # 고정 단계(의도분류)는 변동상세에 안 나와야 함
    assert not any(row[1] == "의도분류(intent)" for row in rows[1:])


def test_stages_sheet_one_row_per_call():
    q = "임플란트 지원"
    recs = assign_rounds([
        _rec("2026-05-26T10:00:01", q),
        _rec("2026-05-26T10:00:02", q),
    ])
    wb = build_workbook(recs)
    ws = wb["stages"]
    # 헤더 1 + 데이터 2
    assert ws.max_row == 3
