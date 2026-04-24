################################
## 수정일 : 2026-03-11
## 마리너 검색시 where 조건에 YEAR 추가하여 검색 정확도 향상
################################

import jpype
from jpype import JString
import glob
import os
import logging
import re
from core.config import Config

logger = logging.getLogger(__name__)

mariner_ip = Config.MARINER_IP
mariner_port = str(Config.MARINER_PORT)
timeout = Config.MARINER_TIMEOUT
threshold = Config.MARINER_THRESHOLD
top_n = Config.RAG_NUM_REFERENCED_DOCS

# 경상남도 시군 이름 정규화 테이블
_SIGUN_NORMALIZE_MAP = {
    # 시
    "창원": "경상남도 창원시",
    "진주": "경상남도 진주시",
    "통영": "경상남도 통영시",
    "사천": "경상남도 사천시",
    "김해": "경상남도 김해시",
    "밀양": "경상남도 밀양시",
    "거제": "경상남도 거제시",
    "양산": "경상남도 양산시",
    "마산": "경상남도 창원시",  # 마산은 창원시에 통합되었으므로 창원으로 매핑
    # 군
    "의령": "경상남도 의령군",
    "함안": "경상남도 함안군",
    "창녕": "경상남도 창녕군",
    "고성": "경상남도 고성군",
    "남해": "경상남도 남해군",
    "하동": "경상남도 하동군",
    "산청": "경상남도 산청군",
    "함양": "경상남도 함양군",
    "거창": "경상남도 거창군",
    "합천": "경상남도 합천군",
}

def normalize_sigun(sigun: str) -> str:
    """부분 시군 이름을 'DB 저장 형태(경상남도 OO시/군)'로 정규화"""
    if not sigun:
        return sigun
    s = sigun.strip()
    if s in {"경남", "경남도", "경상남"}:
        return "경상남도"
    # 이미 완전한 형태면 그대로 반환
    if s.startswith("경상남도 "):
        return s
    if s == "경상남도":
        return s
    # 시/군 접미사 제거 후 매핑 시도 (예: '창원시' → '창원')
    key = re.sub(r'[시군]$', '', s)
    return _SIGUN_NORMALIZE_MAP.get(key, s)

_LIFECYCLE_KEYWORDS = {
    "영유아": ["영유아"],
    "아동·청소년": ["아동", "청소년"],
    "청년": ["청년"],
    "중장년": ["중장년"],
    "노인": ["노인", "노년"],
}

def mariner_search(question, collection_name, sigun, lifecycle: str = "", intent: str = None, project_name: str = None):
    sigun = normalize_sigun(sigun)
    if not sigun:
        logger.warning("sigun이 없어 문서 검색을 수행하지 않습니다.")
        return []

    doc_year_list = []
    doc_sigun_list = []
    doc_score_list = []
    doc_chunk_id_list = []
    doc_content_list = []
    doc_life_cycle_list = []
    doc_business_name_list = []
    doc_department_list = []

    def __get_jar_files():
        jar_files = glob.glob(os.path.join(Config.JAR_LIB_PATH, '*.jar'))
        return jar_files

    jar_files = __get_jar_files()
    if not jpype.isJVMStarted():
        jpype.startJVM(
            jpype.getDefaultJVMPath(),
            "-Djava.class.path={classpath}".format(classpath=":".join(jar_files)),
            convertStrings=True,
        )

    jpkg = jpype.JPackage("com.diquest.ir5.client.command")
    command = jpkg.CommandSearchRequest(mariner_ip, int(mariner_port))
    command.setProps(mariner_ip, int(mariner_port), timeout, 100, 100)

    jpkg = jpype.JPackage("com.diquest.ir5.common.msg.protocol.query")
    query = jpkg.Query("", "")
    startnum = 0
    max_top_n = Config.MARINER_MAX_RESULTS
    endnum = max_top_n - 1
    query.setResult(startnum, endnum)
    query.setFrom(collection_name)
    query.setSearch(True)
    query.setDebug(True)
    query.setPrintQuery(True)
    query.setLoggable(True)
    query.setValue("VS_THRESHOLD", str(threshold))
    query.setValue("VS_RESULT_SIZE", str(max_top_n))
    num = 16

    doc_year_field = jpkg.SelectSet(JString("YEAR"), num, 0)
    doc_sigun_field = jpkg.SelectSet(JString("SIGUN"), num, 0)
    doc_chunk_id_field = jpkg.SelectSet(JString("CHUNK_ID"), num, 0)
    weight_field = jpkg.SelectSet(JString("WEIGHT"), num, 0)
    doc_content_field = jpkg.SelectSet(JString("CONTENT"), num, 0) ## 추가
    dic_life_cycle_field = jpkg.SelectSet(JString("LIFE_CYCLE"), num, 0) ## 추가
    dic_business_name_field = jpkg.SelectSet(JString("BUSINESS_NAME"), num, 0) ## 추가
    dic_department_field = jpkg.SelectSet(JString("DEPARTMENT"), num, 0) ## 추가

    select_set_array = [doc_chunk_id_field, doc_year_field, doc_sigun_field, doc_content_field, weight_field, dic_life_cycle_field, dic_business_name_field, dic_department_field] ## 추가
    query.setSelect(select_set_array)

    # 시군 존재 여부에 따라 BUSINESS_NAME_MI 가중치 변경 (없을 때 0.7, 있을 때 2)
    biz_name_mi_weight = 2 if sigun else 0.7

    # sigun을 질문에 포함시켜 벡터 검색이 해당 시군 문서를 우선 반환하도록 유도
    question_with_sigun = f"{sigun} {question}"
    keyword_string = JString(question_with_sigun)
    where_set_array = [
        jpkg.WhereSet(9),
        jpkg.WhereSet("BUSINESS_NAME_KO", 2,  keyword_string, 0.7),
        jpkg.WhereSet(6),
        jpkg.WhereSet("TEXT_CHUNK_KO",    2,  keyword_string, 0.7),
        jpkg.WhereSet(6),
        jpkg.WhereSet("BUSINESS_NAME_MI", 2,  keyword_string, biz_name_mi_weight),
        jpkg.WhereSet(6),
        jpkg.WhereSet("TEXT_CHUNK_MI",    96, keyword_string, 0.3),
        jpkg.WhereSet(6),
        jpkg.WhereSet("SIGUN",            96, keyword_string, 0.1),
        jpkg.WhereSet(10),
    ]

    # 시군 조건 스크립틀릿
    if sigun:
        where_set_array += [
            jpkg.WhereSet(5),
            jpkg.WhereSet("SIGUN", 33, sigun, 0),
        ]


    query.setWhere(where_set_array)

    queryset = jpkg.QuerySet(1)
    queryset.addQuery(query)
    # print(queryset)

    order_set_array = []
    # order_set_array.append(jpkg.OrderBySet(True, "WEIGHT")) # 검색 정확도 순 정렬
    order_set_array.append(jpkg.OrderBySet(True, "WEIGHT", jpype.JByte(97)))
    query.setOrderby(order_set_array)

    ret = command.request(queryset)
    if ret < 0:
        logger.error(f"Mariner 오류: {ret}")
        return []

    resultSet = command.getResultSet()
    if resultSet is None:
        logger.error("Mariner 서버에서 결과를 받지 못했습니다.")
        return []

    result = resultSet.getResult(0)
    result_size = result.getRealSize()
    logger.debug(f"Mariner 전체 검색 결과: {result_size}개")
    target_sigun = str(sigun).strip()
    target_lifecycle = str(lifecycle or "").strip()
    sample_siguns = list({str(result.getResult(i, 2)).strip() for i in range(min(10, result_size))})
    logger.debug(f"[SIGUN 샘플] target={target_sigun!r}, 실제값(최대10개)={sample_siguns}")
    for i in range(result_size):
        result_sigun = str(result.getResult(i, 2)).strip()
        result_business_name = str(result.getResult(i, 6)).strip()

        is_match = (
            result_sigun.startswith("경상남도")
            if target_sigun == "경상남도"
            else result_sigun == target_sigun
        )
        if not is_match:
            continue

        # LIFE_CYCLE 필터 (파라미터가 있을 때만 적용)
        if target_lifecycle:
            result_lifecycle = str(result.getResult(i, 5)).strip()
            lc_keywords = _LIFECYCLE_KEYWORDS.get(target_lifecycle, [target_lifecycle])
            if not any(kw in result_lifecycle for kw in lc_keywords):
                continue

        # intent가 comparison이고 project_name이 있을 때 사업명 기준 필터
        if intent == "comparison" and project_name:
            if result_business_name != project_name:
                continue

        doc_chunk_id_list.append(str(result.getResult(i, 0)))
        doc_year_list.append(str(result.getResult(i, 1)))
        doc_sigun_list.append(str(result.getResult(i, 2)))
        doc_content_list.append(str(result.getResult(i, 3)))
        doc_life_cycle_list.append(str(result.getResult(i, 5)))  ## 추가
        doc_business_name_list.append(str(result.getResult(i, 6)))  ## 추가
        doc_department_list.append(str(result.getResult(i, 7)))  ## 추가
        doc_score = str(result.getResult(i, 4))  # index 4 = WEIGHT
        doc_score_list.append(doc_score)

    logger.debug(f"SIGUN 일치 결과: {len(doc_chunk_id_list)}개 (LIFE_CYCLE 필터: '{target_lifecycle or '미적용'}')")

    # 원하는 형태로 반환
    return [
        {
            "chunk_id": doc_chunk_id_list[i],
            "year": doc_year_list[i],
            "sigun": doc_sigun_list[i],
            "content": doc_content_list[i],
            "score": doc_score_list[i],
            "life_cycle": doc_life_cycle_list[i],  ## 추가
            "business_name": doc_business_name_list[i],  ## 추가
            "department": doc_department_list[i],  ## 추가
        }
        for i in range(min(len(doc_chunk_id_list), int(top_n)))
    ]

def welfare_tel_search(keyword: str, sigun: str, eupmyeondong: str = "") -> list:
    """GSND_OUR_REGION_TEL 마리너 컬렉션에서 센터명/주소로 연락처 조회.

    Args:
        keyword: 검색 키워드 (센터명 또는 주소)
        sigun: 시군명 (정규화 전 형태도 허용)
        eupmyeondong: 읍면동명 (선택)

    Returns:
        [{"SIGUN": ..., "CENTER": ..., "EUPMYEONDONG": ..., "TEL": ..., "ADDRESS": ...}, ...]
    """
    if not keyword:
        return []

    target_sigun = normalize_sigun(sigun)

    jar_files = glob.glob(os.path.join(Config.JAR_LIB_PATH, '*.jar'))
    if not jpype.isJVMStarted():
        jpype.startJVM(
            jpype.getDefaultJVMPath(),
            "-Djava.class.path={classpath}".format(classpath=":".join(jar_files)),
            convertStrings=True,
        )

    jpkg_cmd = jpype.JPackage("com.diquest.ir5.client.command")
    command = jpkg_cmd.CommandSearchRequest(mariner_ip, int(mariner_port))
    command.setProps(mariner_ip, int(mariner_port), 20000, 100, 100)

    jpkg = jpype.JPackage("com.diquest.ir5.common.msg.protocol.query")
    query = jpkg.Query("", "")
    max_top_n = Config.MARINER_MAX_RESULTS
    query.setResult(0, max_top_n - 1)
    query.setFrom(Config.RAG_WELFARE_TEL_COLLECTION)
    query.setSearch(True)
    query.setDebug(True)
    query.setPrintQuery(True)
    query.setLoggable(True)
    query.setValue("VS_THRESHOLD", str(Config.MARINER_THRESHOLD))
    query.setValue("VS_RESULT_SIZE", str(max_top_n))

    num = 16
    # SelectSet 순서: 0=ID, 1=SIGUN, 2=CENTER, 3=EUPMYEONDONG, 4=TEL, 5=ADDRESS
    select_set_array = [
        jpkg.SelectSet(JString("ID"),           num, 0),
        jpkg.SelectSet(JString("SIGUN"),         num, 0),
        jpkg.SelectSet(JString("CENTER"),        num, 0),
        jpkg.SelectSet(JString("EUPMYEONDONG"),  num, 0),
        jpkg.SelectSet(JString("TEL"),           num, 0),
        jpkg.SelectSet(JString("ADDRESS"),       num, 0),
        jpkg.SelectSet(JString("WEIGHT"),        num, 0),
    ]
    query.setSelect(select_set_array)

    keyword_string = JString(keyword)
    where_set_array = [
        jpkg.WhereSet(9),
        jpkg.WhereSet("CENTER",  2, keyword_string, 0.7),
        jpkg.WhereSet(6),
        jpkg.WhereSet("ADDRESS", 1, keyword_string, 0.7),
        jpkg.WhereSet(10),
    ]

    # SIGUN 스크립틀릿 미적용: OUR_REGION_TEL은 구(區) 단위 SIGUN 저장으로 포맷 불일치 발생.
    # EUPMYEONDONG + 키워드 검색으로 충분.

    if eupmyeondong:
        where_set_array += [
            jpkg.WhereSet(5),
            jpkg.WhereSet("EUPMYEONDONG", 1, eupmyeondong, 0),
        ]

    query.setWhere(where_set_array)

    order_set_array = [jpkg.OrderBySet(False, "WEIGHT", jpype.JByte(97))]
    query.setOrderby(order_set_array)

    queryset = jpkg.QuerySet(1)
    queryset.addQuery(query)

    ret = command.request(queryset)
    if ret < 0:
        logger.error(f"[OurRegionTelMariner] 마리너 오류: {ret}")
        return []

    resultSet = command.getResultSet()
    if resultSet is None:
        logger.error("[OurRegionTelMariner] 마리너 서버에서 결과를 받지 못했습니다.")
        return []

    result = resultSet.getResult(0)
    result_size = result.getRealSize()
    logger.debug(f"[OurRegionTelMariner] 전체 검색 결과: {result_size}개")

    results = []
    for i in range(result_size):
        results.append({
            "SIGUN":        str(result.getResult(i, 1)).strip(),
            "CENTER":       str(result.getResult(i, 2)).strip(),
            "EUPMYEONDONG": str(result.getResult(i, 3)).strip(),
            "TEL":          str(result.getResult(i, 4)).strip(),
            "ADDRESS":      str(result.getResult(i, 5)).strip(),
        })

    logger.info(f"[OurRegionTelMariner] '{keyword}'/{target_sigun} → {len(results)}건")
    return results


# 사용 예시
if __name__ == "__main__":
    # collection_name = get_collection_name_from_prompt()
    question = "받을 수 있는 복지 알려줘"
    # docs = mariner_search(question, collection_name)
    docs = mariner_search(question, 'TEST_OKMS_V3', "경남", "아동")
    for doc in docs:
        print(doc)