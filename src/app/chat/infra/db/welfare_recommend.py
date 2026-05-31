"""복지서비스 추천 — okms2 두 뷰(VIEW_WLF_SRVC + VIEW_GOV_WLF_SRVC) MySQL 직접조회.

라우팅 재설계(A 추천)의 DB 검색 모듈. Mariner 대신 구조화 WHERE로 조회한다.
- 슬롯(시군/생애주기/가구상황/주제/배제어)을 받아 두 뷰를 조회·병합.
- 핵심토큰 LIKE(표기변형 흡수), 전연령(7태그) 제외, 주제 keyword 우선,
  가구상황 (S OR 일반가구) + 그룹 분할, 배제어 NOT LIKE, 사업명 dedup, 0건 폴백.
- 출력: 프론트 GuideServiceItem 호환 카드(+ topic, household_group) + topic_chips.

설계 근거: ROUTING_REDESIGN_PLAN.md §3. DB 접속은 welfare.py 패턴(Config) 재사용.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from app.core.config import Config

logger = logging.getLogger(__name__)

# ───────────────────── 핵심토큰 매핑(공백·가운뎃점·접미사 변형 흡수) ─────────────────────
def _lct(x: str) -> str:
    """생애주기 라벨 → 매칭 토큰. 임신·출산/임신 · 출산 표기흔들림 → '임신'. (노인→노년 통일)"""
    x = "노년" if x == "노인" else x
    return "임신" if "임신" in x else x

_HH_TOKEN = {"한부모·조손": "한부모", "다문화·탈북민": "다문화"}
def _hht(x: str) -> str:
    return _HH_TOKEN.get(x, x)

# 주제 category → 모호하지 않은 핵심토큰 1개(양 뷰 공통). 신체/정신건강은 '건강' 충돌 → full 유지.
_CAT_TOKEN = {
    "주거": "주거", "일자리": "일자리", "보육": "보육", "교육": "교육",
    "신체건강": "신체건강", "정신건강": "정신건강", "생활지원": "생활지원", "법률": "법률",
    "보호돌봄": "돌봄", "안전위기": "안전", "임신출산": "임신", "문화여가": "문화",
    "금융": "금융", "에너지": "에너지", "입양위탁": "입양", "기타": "기타",
}

_LC_TAGS_LOCAL = "(LENGTH(LFTM_CYCL_NM)-LENGTH(REPLACE(LFTM_CYCL_NM,',','')))"  # 쉼표수(=태그수-1)
_LC_TAGS_GOV = "(LENGTH(life_array)-LENGTH(REPLACE(life_array,',','')))"


def _connect():
    import mysql.connector
    return mysql.connector.connect(
        host=Config.DB_HOST, port=Config.DB_PORT,
        user=Config.DB_USER, password=Config.DB_PASSWORD,
        database=Config.OKMS2_DB_NAME, autocommit=True,
        connection_timeout=getattr(Config, "DB_CONNECTION_TIMEOUT", 5),
    )


def _target_year(cur, table: str, col: str) -> str:
    """현재연도, 없으면 그 테이블의 MAX(연도≤현재)로 폴백(빈결과 방지)."""
    cur.execute(f"SELECT MAX(`{col}`) FROM `{table}` WHERE `{col}` <= YEAR(CURDATE())")
    r = cur.fetchone()[0]
    if r:
        return str(r)
    cur.execute(f"SELECT MAX(`{col}`) FROM `{table}`")
    return str(cur.fetchone()[0])


# ───────────────────── WLF_SRVC_CN(라벨 구조 텍스트) 파서 ─────────────────────
_CN_LABELS = ["지원내용", "지원대상", "제공유형", "근 거", "근거", "신청기간", "문의처"]
def _parse_cn(text: Optional[str]) -> Dict[str, str]:
    """'지원내용 : ... 지원대상 : ...' 형태 본문에서 라벨별 값 추출."""
    out: Dict[str, str] = {}
    if not text:
        return out
    # 라벨 위치 찾아 다음 라벨 전까지 슬라이스
    pat = re.compile(r"(지원내용|지원대상|제공유형|근\s*거|신청기간|문의처)\s*[:：]\s*")
    matches = list(pat.finditer(text))
    for i, m in enumerate(matches):
        key = re.sub(r"\s+", "", m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[key] = text[start:end].strip()
    return out


# ───────────────────── 주제 절(keyword 우선, 핵심토큰) ─────────────────────
def _topic_clause(cats: List[str], kws: List[str], gov: bool):
    if kws:
        col = "serv_nm" if gov else "WLF_SRVC_NM"
        return ("(" + " OR ".join([f"{col} LIKE %s"] * len(kws)) + ")", [f"%{k}%" for k in kws])
    if cats:
        ors, ps = [], []
        for c in cats:
            tok = _CAT_TOKEN.get(c, c)
            if gov:
                ors.append("intrs_thema_array LIKE %s"); ps.append(f"%{tok}%")
            else:
                ors.append("ITRST_TPC1 LIKE %s OR ITRST_TPC2 LIKE %s"); ps += [f"%{tok}%", f"%{tok}%"]
        if ors:
            return ("(" + " OR ".join(ors) + ")", ps)
    return None


def _build_local(sigun, lifecycle, household, cats, kws, must_not, year, drop_life=False):
    conds, params = ["WLF_YR=%s"], [year]
    life_active = bool(lifecycle and not drop_life)
    if sigun:
        conds.append("(SIGUN_NM=%s OR SIGUN_NM='경상남도')"); params.append(sigun)
    for ex in (must_not or []):
        conds.append("WLF_SRVC_NM NOT LIKE %s"); params.append(f"%{ex}%")
    if life_active:
        conds.append("(" + " OR ".join(["LFTM_CYCL_NM LIKE %s"] * len(lifecycle)) + ")")
        params += [f"%{_lct(x)}%" for x in lifecycle]
        conds.append(f"{_LC_TAGS_LOCAL} < 6")  # 전연령(7태그) 제외
    if household:
        hh = " OR ".join(["HSHD_STTN_NM LIKE %s"] * len(household))
        conds.append(f"(({hh}) OR HSHD_STTN_NM LIKE %s)")
        params += [f"%{_hht(x)}%" for x in household] + ["%일반가구%"]
    tc = _topic_clause(cats, kws, gov=False)
    if tc:
        conds.append(tc[0]); params += tc[1]
    sql = (
        "SELECT WLF_SRVC_NM, MAX(PRPS), MAX(APLY_PRD), MAX(TELNO), MAX(TKCG_DEPT), "
        "MAX(WLF_SRVC_CN), MAX(LFTM_CYCL_NM), MAX(HSHD_STTN_NM), MAX(IFNULL(ITRST_TPC1,'기타')), "
        f"MAX(ORG_NM), MIN({_LC_TAGS_LOCAL}), MAX(IFNULL(ITRST_TPC2,'')) "
        f"FROM VIEW_WLF_SRVC WHERE {' AND '.join(conds)} GROUP BY WLF_SRVC_NM"
    )
    return sql, params


def _build_gov(lifecycle, household, cats, kws, must_not, year, drop_life=False):
    conds, params = ["crtr_yr=%s"], [year]
    life_active = bool(lifecycle and not drop_life)
    for ex in (must_not or []):
        conds.append("serv_nm NOT LIKE %s"); params.append(f"%{ex}%")
    if life_active:
        conds.append("(" + " OR ".join(["life_array LIKE %s"] * len(lifecycle)) + ")")
        params += [f"%{_lct(x)}%" for x in lifecycle]
        conds.append(f"{_LC_TAGS_GOV} < 6")
    if household:
        hh = " OR ".join(["trgter_indvdl_array LIKE %s"] * len(household))
        conds.append(f"(({hh}) OR trgter_indvdl_array LIKE %s)")
        params += [f"%{_hht(x)}%" for x in household] + ["%일반가구%"]
    tc = _topic_clause(cats, kws, gov=True)
    if tc:
        conds.append(tc[0]); params += tc[1]
    sql = (
        "SELECT serv_nm, MAX(wlfare_info_outl_cn), MAX(tgtr_dtl_cn), MAX(alw_serv_cn), "
        "MAX(rprs_ctadr), MAX(jur_mnof_nm), MAX(trgter_indvdl_array), "
        "MAX(IFNULL(intrs_thema_array,'기타')) "
        f"FROM VIEW_GOV_WLF_SRVC WHERE {' AND '.join(conds)} GROUP BY serv_nm"
    )
    return sql, params


def _household_group(hshd: str, household: List[str]) -> str:
    """우선/더보기 그룹 판정 (§3-5-1). 명시→S매칭 우선·일반가구 더보기 / 미명시→일반가구 우선·특정대상 더보기."""
    hshd = hshd or ""
    if household:  # 명시
        return "우선" if any(_hht(x) in hshd for x in household) else "더보기"
    return "우선" if "일반가구" in hshd else "더보기"


# GOV 주제 표기 → 지역 canonical 라벨로 통일(chip 분리 방지)
_GOV_TOPIC_CANON = {
    "서민금융": "금융", "보호·돌봄": "보호돌봄", "안전·위기": "안전위기",
    "임신·출산": "임신출산", "문화·여가": "문화여가", "입양·위탁": "입양위탁",
}
def _gov_topic(theme: str) -> str:
    """GOV intrs_thema_array(다중값)에서 대표 주제 토큰 1개 → canonical 라벨."""
    if not theme or theme == "기타":
        return "기타"
    first = re.sub(r"\s", "", theme.split(",")[0].strip())  # '보호·돌봄' 공백 제거
    return _GOV_TOPIC_CANON.get(first, first)


def search_recommend(
    sigun: Optional[str],
    lifecycle: Optional[List[str]] = None,
    household: Optional[List[str]] = None,
    topic_category: Optional[List[str]] = None,
    topic_keyword: Optional[List[str]] = None,
    must_not: Optional[List[str]] = None,
    year: Optional[str] = None,
) -> Dict[str, Any]:
    """A 추천 DB 조회. 슬롯 → 두 뷰 검색 → 카드 배열 + topic_chips 반환.

    Returns:
        {
          "year": {"local": .., "gov": ..}, "fallback": "" | "생애주기제거",
          "cards": [ {service_name, purpose, target, benefit, application_period,
                      application_contact, referenced_documents_name,
                      topic, household_group("우선"|"더보기"), source("local"|"gov")} ... ],
          "topic_chips": [ {"label": 주제, "count": n} ... ],  # 건수 많은 순
          "total": n,
        }
    """
    lifecycle = lifecycle or []
    household = [x for x in (household or []) if x != "일반가구"]  # 특정계층만
    topic_category = topic_category or []
    topic_keyword = topic_keyword or []
    must_not = must_not or []

    conn = _connect()
    try:
        cur = conn.cursor()
        yL = _target_year(cur, "VIEW_WLF_SRVC", "WLF_YR")
        yG = _target_year(cur, "VIEW_GOV_WLF_SRVC", "crtr_yr")

        # 지역 + 0건 폴백(생애주기 제거)
        sqlL, pL = _build_local(sigun, lifecycle, household, topic_category, topic_keyword, must_not, yL)
        cur.execute(sqlL, pL); rowsL = cur.fetchall()
        fallback = ""
        if not rowsL and lifecycle:
            sqlL, pL = _build_local(sigun, lifecycle, household, topic_category, topic_keyword, must_not, yL, drop_life=True)
            cur.execute(sqlL, pL); rowsL = cur.fetchall()
            fallback = "생애주기제거"

        sqlG, pG = _build_gov(lifecycle, household, topic_category, topic_keyword, must_not, yG)
        cur.execute(sqlG, pG); rowsG = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    cards: List[Dict[str, Any]] = []
    for (name, prps, aply, tel, dept, cn, lc, hshd, tpc, org, _t, tpc2) in rowsL:
        sec = _parse_cn(cn)
        # topics 멤버십 = TPC1·TPC2(드릴다운 LIKE와 동일 기준). 다주제는 각 분야에 모두 속함.
        topics = []
        for t in (tpc, tpc2):
            t = (t or "").strip()
            if t and t not in topics:
                topics.append(t)
        if not topics:
            topics = ["기타"]
        cards.append({
            "service_name": name or "",
            "purpose": (prps or "").strip(),
            "target": sec.get("지원대상", "") or (hshd or ""),
            "benefit": sec.get("지원내용", ""),
            "application_period": (aply or "").strip(),
            "application_contact": (tel or "").strip() + (f" ({dept})" if dept else ""),
            "referenced_documents_name": org or "",
            "topic": topics[0],
            "topics": topics,
            "household_group": _household_group(hshd, household),
            "source": "local",
        })
    for (name, outl, tgt, alw, ctadr, mnof, trg, theme) in rowsG:
        # GOV theme 다중값 → canonical 토큰 멤버십(드릴다운 intrs_thema LIKE와 동일)
        topics = []
        for t in str(theme or "").split(","):
            tok = _gov_topic(t)
            if tok and tok not in topics:
                topics.append(tok)
        if not topics:
            topics = ["기타"]
        cards.append({
            "service_name": name or "",
            "purpose": (outl or "").strip(),
            "target": (tgt or trg or "").strip(),
            "benefit": (alw or "").strip(),
            "application_period": "",
            "application_contact": (ctadr or "").strip() + (f" ({mnof})" if mnof else ""),
            "referenced_documents_name": "",
            "topic": topics[0],
            "topics": topics,
            "household_group": _household_group(trg, household),
            "source": "gov",
        })

    # topic_chips: 주제 멤버십 카드 수(드릴다운과 동일 기준). 다주제 카드는 각 분야에 카운트.
    chip_count: Dict[str, int] = {}
    for c in cards:
        for t in c.get("topics", [c["topic"]]):
            chip_count[t] = chip_count.get(t, 0) + 1
    topic_chips = [{"label": k, "count": v} for k, v in sorted(chip_count.items(), key=lambda kv: -kv[1])]

    logger.info(
        "[WelfareRecommend] sigun=%s lc=%s hh=%s cat=%s kw=%s must_not=%s → local=%d gov=%d fallback=%s",
        sigun, lifecycle, household, topic_category, topic_keyword, must_not,
        len(rowsL), len(rowsG), fallback or "-",
    )
    return {
        "year": {"local": yL, "gov": yG},
        "fallback": fallback,
        "cards": cards,
        "topic_chips": topic_chips,
        "total": len(cards),
    }


if __name__ == "__main__":  # 스모크 테스트
    import json
    for kw in (
        dict(sigun="경상남도 창원시", lifecycle=["노년"]),
        dict(sigun="경상남도 창원시", lifecycle=["청년"], topic_category=["주거"]),
        dict(sigun="경상남도 고성군", lifecycle=["노년"], topic_keyword=["임플란트"]),
        dict(sigun="경상남도 의령군", household=["저소득"], must_not=["의료급여"]),
    ):
        r = search_recommend(**kw)
        print("IN:", kw)
        print(f"  total={r['total']} fallback={r['fallback']} chips={[(c['label'],c['count']) for c in r['topic_chips'][:6]]}")
        if r["cards"]:
            c = r["cards"][0]
            print(f"  카드예: {c['service_name']} | 대상={c['target'][:25]} | 내용={c['benefit'][:30]} | 그룹={c['household_group']} | 주제={c['topic']} | {c['source']}")
        print()
