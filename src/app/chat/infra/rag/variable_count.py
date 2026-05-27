"""guide_recommend 가변 개수 선택 — 순수 함수 (서버·DB·LLM 비의존, 유닛테스트 대상).

설계 의도(plans/hazy-knitting-lark.md Part E):
- 기존 "고정 top-N(=항상 8~11 채움)"이 관련 풀이 작은 질의에서도 무관 문서를 패딩하는 문제.
- 대신 ① reformed/keywords 에서 상황·일반어를 뺀 **주제어**를 뽑고
  ② 주제어가 문서에 하나도 없으면 명백 무관으로 보고 컷(결정적 relevance gate),
  ③ 점수(rrf_score|WEIGHT) 임계로 꼬리를 자르고 MIN~MAX 로 클램프한다.
- 주제어가 없으면(광역 질의: "노인 복지 추천") 변별 불가 → 점수 기반 soft top-N 로 동작.

동의어 맵은 만들지 않는다: 검색 단계가 이미 Mariner 동의어 확장을 하므로 관련 문서는 풀에
들어와 있고, 여기서는 "존재 여부"만 본다. 과제거가 관측되면 그때 반응형으로 보강한다.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

# 상황·일반어 — 주제 변별에 기여하지 않으므로 topic_terms 에서 제거한다.
# (생애주기/가구상황/일반 복지어/금액 단위). 지역명은 호출부에서 exclude 로 전달.
_TOPIC_STOPWORDS: frozenset[str] = frozenset({
    # 생애주기
    "영유아", "영아", "유아", "아동", "어린이", "청소년", "청년", "중장년", "장년",
    "노인", "어르신", "고령", "임산부", "임신부", "산모",
    # 가구·상황
    "가구", "세대", "1인", "일인", "독거", "다문화", "탈북민", "북한이탈주민",
    "한부모", "조손", "기초생활", "수급자", "저소득",
    # 일반 복지어
    "복지", "지원", "서비스", "사업", "제도", "정책", "프로그램", "혜택",
    "안내", "추천", "대상", "신청", "제공", "관련", "도움", "받을",
    # 금액·수량 단위
    "소득", "월", "원", "만원", "연", "세", "이상", "미만", "정도",
})

# 문서에서 주제어 매칭에 쓸 텍스트 필드 (사업명·서비스명·본문).
_BLOB_KEYS: tuple[str, ...] = (
    "BUSINESS_NAME", "SERVICE_NAME", "NAME", "name", "title",
    "BUSINESS_NAME_KO", "SERVICE_NAME_KO",
    "CONTENT", "TEXT_CHUNK", "TEXT_CHUNK_KO", "PURPOSE", "_snippet",
)

# 행정구역 접미사 — 시군명 비교 시 정규화한다.
# exclude 는 정규화된 시군명("창원시")으로 오는데 keywords 는 명사추출이 접미사를 떼어
# "창원"으로 주므로, 접미사를 무시하고 비교하지 않으면 지역명이 주제어로 새어든다.
_REGION_SUFFIX: tuple[str, ...] = ("시", "군", "구")


def _strip_region_suffix(s: str) -> str:
    if len(s) > 1 and s.endswith(_REGION_SUFFIX):
        return s[:-1]
    return s


def extract_topic_terms(
    keywords: Sequence[str],
    *,
    exclude: Sequence[str] = (),
) -> List[str]:
    """keywords 에서 상황·일반어·지역명·숫자토큰을 제거한 변별 주제어를 반환.

    예) ["진주","청년","가구","소득","월세","지원"] + exclude=["진주"] → ["월세"]
    """
    excl = set(_TOPIC_STOPWORDS)
    for e in exclude or ():
        e = (e or "").strip()
        if e:
            excl.add(e)
            excl.add(_strip_region_suffix(e))  # "창원시" → "창원" 도 함께 제외
    out: List[str] = []
    seen: set[str] = set()
    for k in keywords or ():
        k = (k or "").strip()
        if not k or k in excl or _strip_region_suffix(k) in excl:
            continue
        if len(k) < 2:
            continue
        if any(ch.isdigit() for ch in k):
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def _doc_blob(doc: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in _BLOB_KEYS:
        v = doc.get(key)
        if v:
            parts.append(str(v))
    return " ".join(parts).casefold()


def topical_hit(doc: Dict[str, Any], topic_terms: Sequence[str]) -> int:
    """문서에 등장하는 주제어 개수 (부분일치, 대소문자 무시)."""
    if not topic_terms:
        return 0
    blob = _doc_blob(doc)
    if not blob:
        return 0
    return sum(1 for t in topic_terms if t and t.casefold() in blob)


def _score_getter(docs: Sequence[Dict[str, Any]]):
    """점수 필드 결정: rrf_score 가 하나라도 있으면 rrf_score, 아니면 WEIGHT."""
    use_rrf = any(float(d.get("rrf_score") or 0) for d in docs)
    key = "rrf_score" if use_rrf else "WEIGHT"

    def _score(d: Dict[str, Any]) -> float:
        try:
            return float(d.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    return _score


def _tail_cut(
    ranked: List[Dict[str, Any]],
    score,
    *,
    keep_ratio: float,
    gap_drop: float,
    max_results: int,
) -> List[Dict[str, Any]]:
    """점수 내림차순 ranked 에서 비율 floor·급락 절벽·MAX 로 꼬리를 자른다."""
    if not ranked:
        return []
    top = score(ranked[0]) or 0.0
    kept: List[Dict[str, Any]] = []
    prev = None
    for i, d in enumerate(ranked):
        if i >= max_results:
            break
        s = score(d)
        if top > 0 and s < top * keep_ratio:
            break
        if prev is not None and prev > 0 and s < prev * gap_drop:
            break
        kept.append(d)
        prev = s
    return kept


def select_variable_count(
    docs: List[Dict[str, Any]],
    topic_terms: Sequence[str],
    *,
    keep_ratio: float = 0.55,
    gap_drop: float = 0.6,
    min_results: int = 3,
    max_results: int = 11,
) -> List[Dict[str, Any]]:
    """가변 개수 선택.

    - 주제어 없음(광역 질의): 점수 기반 꼬리컷 + MIN~MAX 클램프.
    - 주제어 있음(변별 가능): 주제어가 문서에 1개 이상인 hit 만 우선 노출(상한 MAX).
      hit 이 MIN 미만이면 점수 상위 miss 로만 MIN 까지 보강.

    docs 는 정렬 안 돼 있어도 됨 — 내부에서 점수 내림차순 정렬.
    원본 doc dict 은 변형하지 않는다.
    """
    if not docs:
        return docs
    score = _score_getter(docs)
    ranked = sorted(docs, key=score, reverse=True)

    if not topic_terms:
        kept = _tail_cut(
            ranked, score,
            keep_ratio=keep_ratio, gap_drop=gap_drop, max_results=max_results,
        )
        if len(kept) < min_results:
            kept = ranked[:min_results]
        return kept[:max_results]

    hits = [d for d in ranked if topical_hit(d, topic_terms) > 0]
    misses = [d for d in ranked if topical_hit(d, topic_terms) == 0]
    kept = hits[:max_results]
    if len(kept) < min_results:
        kept = kept + misses[: min_results - len(kept)]
    return kept[:max_results]
