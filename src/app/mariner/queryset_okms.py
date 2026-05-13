"""
Mariner 쿼리셋 — GSND_BIZ_DATASET_V4 듀얼 검색 (Group A/B)

"""

import logging
import re
import time
from typing import Dict, Any, List, Optional

import jpype
from jpype import JString

from app.core.config import Config
from app.core.constants import (
    MARINER_SELECT_FIELD_NUM,
    MARINER_SETPROPS_EXTRA,
    OP_BRACE_OPEN,
    OP_OR,
    OP_BRACE_CLOSE,
    OP_AND,
    OP_NOT,
    OP_HASANY,
    OP_HASALL,
    OP_INT_SUMMATION,
    MARINER_WEIGHT_HIGH,
    MARINER_WEIGHT_MED,
    MARINER_WEIGHT_LOW,
)
from app.core.exceptions import RAGServiceError
from app.mariner.jvm_manager import ensure_jvm_thread

logger = logging.getLogger(__name__)

# 검색단에 NOT pair 로 적용할 제외 ID 의 상한.
# 초과분은 호출부의 filter_excluded_docs 가 post-filter 로 처리.
# (NOT pair 가 많아지면 Mariner 쿼리 트리가 커져 -60004 타임아웃 발생)
_MARINER_SEARCH_EXCLUSION_CAP = 15

_ANCHOR_STOPWORDS = {
    "지원", "정보", "안내", "신청", "방법", "대상", "조건", "사업",
    "제도", "서비스", "복지", "문의", "내용", "절차", "기준",
}

# 연도/숫자 토큰 패턴: anchor 후보에서 배제 (YEAR FilterSet과 중복 + BUSINESS_NAME에 보통 미수록)
# 예: "2025", "2025년", "26년", "26", "10년", "1년"
_NUMERIC_ANCHOR_RE = re.compile(r"^\d{1,4}(?:년|년도)?$")


def _is_numeric_anchor_token(token: str) -> bool:
    """순수 숫자(연도 포함) 토큰이면 anchor 후보에서 배제."""
    return bool(_NUMERIC_ANCHOR_RE.fullmatch(token or ""))


def _tokenize_query_terms(text: str) -> List[str]:
    tokens: List[str] = []
    for raw in str(text or "").split():
        tok = re.sub(r"[^0-9A-Za-z가-힣]", "", raw).strip()
        if len(tok) < 2:
            continue
        tokens.append(tok)
    return tokens


def _extract_business_anchor(
    vector: str,
    keyword: str,
    sigun_filters: Optional[List[str]],
) -> str:
    """
    질의에서 사업명 정합성 앵커 1개를 추출.
    - vector/keyword 공통 토큰 우선
    - 지역명/일반어/연도(숫자) 토큰은 제외
    """
    vector_tokens = _tokenize_query_terms(vector)
    keyword_tokens = _tokenize_query_terms(keyword)
    if not vector_tokens and not keyword_tokens:
        return ""

    banned: set[str] = set(_ANCHOR_STOPWORDS)
    for s in sigun_filters or []:
        s = str(s or "").strip()
        if not s:
            continue
        banned.add(s)
        banned.add(s.replace("경상남도", "").strip())
        banned.update(t for t in _tokenize_query_terms(s))

    def _is_valid_anchor(tok: str) -> bool:
        if tok in banned:
            return False
        if _is_numeric_anchor_token(tok):
            return False
        return True

    vec_set = {t for t in vector_tokens if _is_valid_anchor(t)}
    key_set = {t for t in keyword_tokens if _is_valid_anchor(t)}

    # 공통 핵심어를 우선 사용(예: "기초연금")
    common = sorted((vec_set & key_set), key=len, reverse=True)
    for tok in common:
        if len(tok) >= 3:
            return tok

    # 공통어가 없으면 vector 쪽에서 가장 긴 토큰 사용
    candidates = sorted(vec_set, key=len, reverse=True)
    for tok in candidates:
        if len(tok) >= 3:
            return tok
    return ""


def _build_okms_document_name(doc: Dict[str, Any]) -> str:
    """OKMS 문서의 UI 표시용 이름 생성 (ORG_NM 우선, 없으면 BUSINESS_NAME, 없으면 SIGUN/YEAR)"""
    org_nm = str(doc.get("ORG_NM", "") or "").strip()
    if org_nm:
        return org_nm
    business_name = str(doc.get("BUSINESS_NAME", "") or "").strip()
    if business_name:
        return business_name
    sigun = str(doc.get("SIGUN", "") or "").strip()
    year = str(doc.get("YEAR", "") or "").strip()
    parts = []
    if sigun:
        parts.append(sigun)
    if year:
        parts.append(year)
    return " / ".join(parts) or str(doc.get("CHUNK_ID", "") or "문서")


# ============================================================
# 듀얼 검색 내부 공통 함수 (Group A/B 공유)
# ============================================================

def _query_dual_documents(
    vector: str,
    keyword: str,
    collection: str = None,
    user_id: Optional[str] = None,
    conv_id: Optional[str] = None,
    year_filters: Optional[List[str]] = None,
    sigun_filters: Optional[List[str]] = None,
    lifecycle_filter: Optional[str] = None,
    excluded_chunk_ids: Optional[List[str]] = None,
    apply_business_anchor: bool = True,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    QuerySet(2) 듀얼 검색 공통 로직 — OKMS 컬렉션 전용.

    Query[0]: keyword(트리플쿼리), Query[1]: vector(확장쿼리)
    KO 필드 가중치 0.7 / MI 필드 가중치 0.3

    Returns:
        (keyword_docs, vector_docs) 튜플
    """
    log_label = "GroupA"
    t1 = time.monotonic()

    if not Config.RAG_ENABLED:
        logger.warning("RAG가 비활성화되어 있습니다.")
        return [], []

    if collection is None:
        collection = Config.RAG_COLLECTION

    excluded_chunk_set = {
        str(chunk_id).strip()
        for chunk_id in (excluded_chunk_ids or [])
        if str(chunk_id).strip()
    }
    if excluded_chunk_set:
        excluded_values = sorted(excluded_chunk_set)
        logger.info(
            "[MoreResults][Mariner/%s] 제외 입력 수=%d | 샘플=%s",
            log_label,
            len(excluded_values),
            excluded_values[:10],
        )

    try:
        timeout = Config.MARINER_TIMEOUT
        threshold = Config.MARINER_THRESHOLD
        max_top_n = Config.MARINER_MAX_RESULTS

        ensure_jvm_thread()

        jpkg_cmd = jpype.JPackage("com.diquest.ir5.client.command")
        command = jpkg_cmd.CommandSearchRequest(Config.MARINER_IP, int(Config.MARINER_PORT))
        command.setProps(Config.MARINER_IP, int(Config.MARINER_PORT), timeout, MARINER_SETPROPS_EXTRA, MARINER_SETPROPS_EXTRA)

        jpkg_query = jpype.JPackage("com.diquest.ir5.common.msg.protocol.query")

        # keyword가 비어 있으면 벡터 전용(QuerySet(1)), 있으면 듀얼(QuerySet(2))
        use_dual = bool(keyword.strip())
        num_queries = 2 if use_dual else 1
        queryset = jpkg_query.QuerySet(num_queries)

        # SELECT 필드 (OKMS 컬렉션 전용)
        num = MARINER_SELECT_FIELD_NUM
        select_field_names = ["ID", "YEAR", "SIGUN", "CONTENT", "WEIGHT", "LIFE_CYCLE", "DEPARTMENT", "APPLICATION_PERIOD", "PURPOSE", "TEL", "BUSINESS_NAME", "ORG_NM", "PATH"]

        normalized_user_id = str(user_id or "").strip()
        normalized_conv_id = str(conv_id or "").strip()
        apply_upload_filter = bool(normalized_user_id and normalized_conv_id)
        if apply_upload_filter:
            select_field_names.extend(["USER_ID", "CONV_ID"])

        select_set_array = [
            jpkg_query.SelectSet(JString(field_name), num, 0)
            for field_name in select_field_names
        ]
        field_indexes = {field_name: idx for idx, field_name in enumerate(select_field_names)}

        # sigun 존재 여부에 따라 BUSINESS_NAME_MI 가중치 변경
        has_sigun = bool(sigun_filters and any(s != "경상남도" for s in sigun_filters))
        # biz_name_mi_weight = 2.0 if has_sigun else MARINER_WEIGHT_HIGH

        # sigun 스크립틀릿용 값 ("경상남도" 제외한 실제 시군 목록)
        sigun_scriptlet_values = [
            s for s in (sigun_filters or [])
            if s and s != "경상남도"
        ]

        # use_dual=True: Query[0]=keyword, Query[1]=vector
        # use_dual=False: Query[0]=vector 전용
        logger.debug(f"[query_okms keywords] keywords = {keyword} / vector = {vector}")
        search_strings = [JString(keyword), JString(vector)] if use_dual else [JString(vector)]

        # KO 필드 가중치 0.7, MI 필드 가중치 0.3
        uniform = {"biz_ko": MARINER_WEIGHT_HIGH, "txt_ko": MARINER_WEIGHT_HIGH, "biz_mi": MARINER_WEIGHT_MED, "txt_mi": MARINER_WEIGHT_MED, "sigun": MARINER_WEIGHT_LOW}
        weight_sets = [uniform] * num_queries

        for i in range(num_queries):
            ks = search_strings[i]
            if not ks or not str(ks).strip():
                logger.info(f"[Mariner/{log_label}] Query #{i} 검색어가 비어 있습니다. 검색을 건너뜁니다.")
                continue
            ws = weight_sets[i]
            query = jpkg_query.Query("", "")

            query.setResult(0, int(max_top_n) - 1)
            query.setSearchKeyword(ks)
            query.setFrom(collection)
            query.setSearch(True)
            query.setDebug(True)
            query.setPrintQuery(True)
            query.setLoggable(True)

            query.setValue("VS_THRESHOLD", str(threshold))
            query.setValue("VS_RESULT_SIZE", str(max_top_n))

            query.setSelect(select_set_array)

            # 정렬: WEIGHT 수치 내림차순 (JByte(97) = 수치 정렬)
            order_set_array = [jpkg_query.OrderBySet(True, "WEIGHT", jpype.JByte(97))]
            query.setOrderby(order_set_array)

            # WHERE: 5개 필드 OR 검색 + 스크립틀릿 필터

            where_set_array = [
                jpkg_query.WhereSet(OP_BRACE_OPEN),                                              # OR (
                jpkg_query.WhereSet("BUSINESS_NAME_KO", OP_HASANY,  ks, ws["biz_ko"]),      #   사업명 키워드
                jpkg_query.WhereSet(OP_OR),                                              #   OR
                # jpkg_query.WhereSet("TEXT_CHUNK_KO",    2,  ks, ws["txt_ko"]),      #   텍스트 키워드
                jpkg_query.WhereSet("TEXT_CHUNK_KO",    2,  ks, ws["txt_mi"]),      #   텍스트 키워드
                jpkg_query.WhereSet(OP_OR),                                              #   OR
                jpkg_query.WhereSet("BUSINESS_NAME_MI", OP_HASANY,  ks, ws["biz_mi"]),      #   사업명 벡터
                jpkg_query.WhereSet(OP_OR),                                              #   OR
                jpkg_query.WhereSet("TEXT_CHUNK_MI",    96, ks, ws["txt_mi"]),      #   텍스트 벡터
                jpkg_query.WhereSet(OP_OR),                                              #   OR
                jpkg_query.WhereSet("SIGUN",            96, ks, ws["sigun"]),       #   시군 벡터
                jpkg_query.WhereSet(OP_BRACE_CLOSE),                                             # )
            ]

            # 사업명 정합성 앵커(하드코딩 없이 질의에서 동적 추출)
            # 예: "창원 기초연금 ..." 질의에서 "기초연금"을 앵커로 사용
            # apply_business_anchor=False(예: fallback)면 앵커 미적용 → 검색 폭 유지
            anchor_term = (
                _extract_business_anchor(
                    vector=str(vector or ""),
                    keyword=str(keyword or ""),
                    sigun_filters=sigun_filters,
                )
                if apply_business_anchor
                else ""
            )
            if anchor_term:
                anchor = JString(anchor_term)
                where_set_array += [
                    jpkg_query.WhereSet(OP_AND),
                    jpkg_query.WhereSet(OP_BRACE_OPEN),
                    jpkg_query.WhereSet("BUSINESS_NAME_KO", OP_HASALL, anchor, MARINER_WEIGHT_HIGH),
                    jpkg_query.WhereSet(OP_OR),
                    jpkg_query.WhereSet("BUSINESS_NAME_MI", OP_HASANY, anchor, MARINER_WEIGHT_HIGH),
                    jpkg_query.WhereSet(OP_BRACE_CLOSE),
                ]
                logger.debug(
                    "[Mariner/%s] 사업명 앵커 적용: %s",
                    log_label,
                    anchor_term,
                )

            # SIGUN 스크립틀릿 필터 (n개 OR)
            if sigun_scriptlet_values:
                if len(sigun_scriptlet_values) == 1:
                    where_set_array += [
                        jpkg_query.WhereSet(OP_AND),
                        jpkg_query.WhereSet("SIGUN", OP_INT_SUMMATION, sigun_scriptlet_values[0], 0),
                    ]
                else:
                    where_set_array.append(jpkg_query.WhereSet(OP_AND))
                    where_set_array.append(jpkg_query.WhereSet(OP_BRACE_OPEN))  # OR (
                    for idx, sv in enumerate(sigun_scriptlet_values):
                        if idx > 0:
                            where_set_array.append(jpkg_query.WhereSet(OP_OR))  # OR
                        where_set_array.append(jpkg_query.WhereSet("SIGUN", OP_INT_SUMMATION, sv, 0))
                    where_set_array.append(jpkg_query.WhereSet(OP_BRACE_CLOSE))  # )

            # LIFE_CYCLE 스크립틀릿 필터
            if lifecycle_filter:
                where_set_array += [
                    jpkg_query.WhereSet(OP_AND),
                    jpkg_query.WhereSet("LIFE_CYCLE", 34, lifecycle_filter, 0),
                ]

            # CHUNK_ID 제외 필터 (예제 패턴: NOT + EXACT 반복)
            # NOT pair 가 많아지면 Mariner 쿼리 트리가 폭증해 -60004(타임아웃) 발생.
            # 검색단은 cap 까지만 적용하고, 잔여는 호출부의 filter_excluded_docs(post-filter)가 처리한다.
            if excluded_chunk_set:
                excluded_values = sorted(excluded_chunk_set)
                _n_total = len(excluded_values)
                search_excluded_values = excluded_values[:_MARINER_SEARCH_EXCLUSION_CAP]
                _n_applied = len(search_excluded_values)
                _overflow = _n_total - _n_applied
                logger.info(
                    "[MoreResults][Mariner/%s] 검색단 제외 IDs 총=%d 적용=%d%s 샘플=%s",
                    log_label,
                    _n_total,
                    _n_applied,
                    f" (cap={_MARINER_SEARCH_EXCLUSION_CAP}, 잔여 {_overflow}건 post-filter)" if _overflow > 0 else "",
                    search_excluded_values[:20],
                )
                for chunk_id in search_excluded_values:
                    where_set_array += [
                        jpkg_query.WhereSet(OP_NOT),
                        jpkg_query.WhereSet("ID", OP_INT_SUMMATION, chunk_id, 0),
                    ]

            query.setWhere(where_set_array)

            # YEAR FilterSet (사용자가 명시한 경우만 필터, 없으면 전체 연도)
            if year_filters:
                filter_years = sorted(set(str(y).strip() for y in year_filters if str(y).strip()))
            else:
                filter_years = []  # 명시 안 하면 필터 없음 (전체 연도)

            if filter_years:
                min_year = filter_years[0]
                max_year = filter_years[-1]
                filter_set_array = [
                    jpkg_query.FilterSet(
                        jpype.JByte(3), "YEAR",
                        jpype.JArray(jpype.JString)([f"{min_year}0101", f"{max_year}1231"]), 0
                    )
                ]
                query.setFilter(filter_set_array)
                logger.debug(f"[Mariner/{log_label}] YEAR FilterSet: {min_year}~{max_year}")

            queryset.addQuery(query)

        ret = command.request(queryset)

        if ret < 0:
            logger.error(f"[Mariner/{log_label}] 요청 오류: {ret}")
            if sigun_filters == None :
                sigun_filters = 'null'
            if lifecycle_filter == None:
                lifecycle_filter = 'null'
            if year_filters == None:
                year_filters = 'null' 
            raise RAGServiceError(f"Mariner API 반환 코드: {ret}")

        resultSet = command.getResultSet()
        if resultSet is None:
            logger.warning(f"[Mariner/{log_label}] 서버에서 결과를 받지 못했습니다.")
            return [], []

        # use_dual=True:  getResult(0)=keyword, getResult(1)=vector
        # use_dual=False: getResult(0)=vector 전용
        keyword_docs = []
        vector_docs = []
        result_pairs = (
            [(0, keyword_docs, "keyword"), (1, vector_docs, "vector")]
            if use_dual else
            [(0, vector_docs, "vector")]
        )
        for result_idx, doc_list_ref, label in result_pairs:
            result = resultSet.getResult(result_idx)
            result_size = result.getRealSize()
            logger.info(f"[Mariner/{log_label}] [{label}] raw 결과: {result_size}개 (필터 전)")
            excluded_count = 0
            raw_id_samples: List[str] = []
            removed_id_samples: List[str] = []

            for i in range(result_size):
                try:
                    raw_weight = result.getResult(i, field_indexes["WEIGHT"])
                    weight_val = float(str(raw_weight)) * 0.0001
                except (ValueError, TypeError):
                    weight_val = 0.0

                # if weight_val < 0.5:
                #     continue

                if apply_upload_filter:
                    result_user_id = str(result.getResult(i, field_indexes["USER_ID"]) or "").strip()
                    result_conv_id = str(result.getResult(i, field_indexes["CONV_ID"]) or "").strip()
                    if result_user_id != normalized_user_id or result_conv_id != normalized_conv_id:
                        continue

                if excluded_chunk_set:
                    result_chunk_id = str(result.getResult(i, field_indexes["ID"]) or "").strip()
                    if result_chunk_id and len(raw_id_samples) < 10:
                        raw_id_samples.append(result_chunk_id)
                    if result_chunk_id and result_chunk_id in excluded_chunk_set:
                        excluded_count += 1
                        if len(removed_id_samples) < 20:
                            removed_id_samples.append(result_chunk_id)
                        continue

                doc = {field_name: str(result.getResult(i, idx) or "") for field_name, idx in field_indexes.items()}
                doc["WEIGHT"] = str(weight_val)

                # OKMS 필드 매핑
                doc["CHUNK_ID"] = doc.get("ID", "")
                doc["NAME"] = _build_okms_document_name(doc)
                doc["CHUNK_PATH"] = str(doc.get("CONTENT", "") or "")

                doc_list_ref.append(doc)

            if excluded_chunk_set:
                logger.info(
                    f"[MoreResults][Mariner/{log_label}] [{label}] CHUNK_ID 1차 제외: {excluded_count}개"
                )
                logger.info(
                    "[MoreResults][Mariner/%s] [%s] raw ID 샘플=%s",
                    log_label,
                    label,
                    raw_id_samples,
                )
                logger.info(
                    "[MoreResults][Mariner/%s] [%s] 실제 제외 ID 샘플=%s",
                    log_label,
                    label,
                    removed_id_samples[:10],
                )

        t2 = time.monotonic()
        logger.info(
            f"[Mariner/{log_label}] 검색 시간: {t2 - t1:.3f}초, "
            f"keyword 결과: {len(keyword_docs)}개, vector 결과: {len(vector_docs)}개"
        )

        return keyword_docs, vector_docs

    except RAGServiceError:
        raise
    except Exception as e:
        logger.error(f"[Mariner/{log_label}] 예상치 못한 오류: {e}", exc_info=True)
        raise RAGServiceError(f"문서 검색 중 오류: {str(e)}") from e


# ============================================================
# Group A — 균등 가중치 듀얼 검색
# ============================================================

def query_group_a_documents(
    vector: str,
    keyword: str,
    collection: str = None,
    user_id: Optional[str] = None,
    conv_id: Optional[str] = None,
    year_filters: Optional[List[str]] = None,
    sigun_filters: Optional[List[str]] = None,
    lifecycle_filter: Optional[str] = None,
    excluded_chunk_ids: Optional[List[str]] = None,
    apply_business_anchor: bool = True,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Group A — 듀얼 검색 (QuerySet(2))

    keyword(트리플쿼리)와 vector(확장쿼리)를 동시에 Mariner에 전송합니다.
    KO 필드 가중치 0.7 / MI 필드 가중치 0.3

    apply_business_anchor:
        - True(기본): 정상 검색에서 사업명 앵커를 BUSINESS_NAME_KO/MI에 강하게 적용
        - False: fallback 등에서 검색 폭을 넓히기 위해 앵커 미적용

    Returns:
        (keyword_docs, vector_docs) 튜플
    """
    return _query_dual_documents(
        vector, keyword, collection,
        user_id=user_id, conv_id=conv_id,
        year_filters=year_filters,
        sigun_filters=sigun_filters,
        lifecycle_filter=lifecycle_filter,
        excluded_chunk_ids=excluded_chunk_ids,
        apply_business_anchor=apply_business_anchor,
    )
