################################
## 수정일 : 2026-03-11
## 마리너 검색시 where 조건에 SIGUN 추가하여 검색 정확도 향상
################################

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import jpype
from jpype import JString
import glob
import logging
import re
from core.config import Config

logger = logging.getLogger(__name__)

mariner_ip = '10.10.10.102'
mariner_port = '5555'
timeout = 60000
threshold = 0.2
top_n = Config.RAG_NUM_REFERENCED_DOCS

def get_collection_name_from_prompt():
    prompt_text = load_collection_category_prompt()
    match = re.search(r'(TEST_OKMS_V3)', prompt_text)
    if match:
        return match.group(1)
    else:
        return 'TEST_OKMS_V3'  # 기본값

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

def mariner_search(question, collection_name, sigun):
    sigun = normalize_sigun(sigun)
    if not sigun:
        logger.warning("sigun이 없어 문서 검색을 수행하지 않습니다.")
        return []

    doc_year_list = []
    doc_sigun_list = []
    doc_score_list = []
    doc_chunk_id_list = []
    doc_content_list = []

    def __get_jar_files():
        lib_path = '/home/diquest/gsnd_rag/backend/jar_lib'
        jar_files = glob.glob(os.path.join(lib_path, '*.jar'))
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
    max_top_n = 500
    endnum = max_top_n - 1
    query.setResult(startnum, endnum)
    query.setFrom(collection_name)
    query.setSearch(True)
    query.setDebug(False)
    query.setPrintQuery(False)
    query.setLoggable(False)
    query.setValue("VS_THRESHOLD", str(threshold))
    query.setValue("VS_RESULT_SIZE", str(max_top_n))
    num = 16

    doc_year_field = jpkg.SelectSet(JString("YEAR"), num, 0)
    doc_sigun_field = jpkg.SelectSet(JString("SIGUN"), num, 0)
    doc_chunk_id_field = jpkg.SelectSet(JString("CHUNK_ID"), num, 0)
    weight_field = jpkg.SelectSet(JString("WEIGHT"), num, 0)
    doc_content_field = jpkg.SelectSet(JString("CONTENT"), num, 0) ## 추가

    select_set_array = [doc_chunk_id_field, doc_year_field, doc_sigun_field, doc_content_field, weight_field] ## 추가
    query.setSelect(select_set_array)

    # sigun을 질문에 포함시켜 벡터 검색이 해당 시군 문서를 우선 반환하도록 유도
    question_with_sigun = f"{sigun} {question}"
    keyword_string = JString(question_with_sigun)
    where_set_array = [
        jpkg.WhereSet("TEXT_CHUNK_KO", 2, keyword_string, 0.01),
        jpkg.WhereSet(6),
        jpkg.WhereSet("TEXT_CHUNK_MI", 96, keyword_string, 0.99)
    ]
    query.setWhere(where_set_array)

    queryset = jpkg.QuerySet(1)
    queryset.addQuery(query)

    order_set_array = []
    order_set_array.append(jpkg.OrderBySet(True, "WEIGHT")) # 검색 정확도 순 정렬
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
    for i in range(result_size):
        result_sigun = str(result.getResult(i, 2)).strip()

        is_match = (
            result_sigun.startswith("경상남도")
            if target_sigun == "경상남도"
            else result_sigun == target_sigun
        )
        if not is_match:
            continue

        doc_chunk_id_list.append(str(result.getResult(i, 0)))
        doc_year_list.append(str(result.getResult(i, 1)))
        doc_sigun_list.append(str(result.getResult(i, 2)))
        doc_content_list.append(str(result.getResult(i, 3)))
        doc_score = str(result.getResult(i, 4))  # index 4 = WEIGHT
        doc_score_list.append(doc_score)

    logger.debug(f"SIGUN 일치 결과: {len(doc_chunk_id_list)}개")

    # 원하는 형태로 반환
    return [
        {
            "chunk_id": doc_chunk_id_list[i],
            "year": doc_year_list[i],
            "sigun": doc_sigun_list[i],
            "content": doc_content_list[i],
            "score": doc_score_list[i]
        }
        for i in range(min(len(doc_chunk_id_list), int(top_n)))
    ]

if __name__ == "__main__":
    question = "경상남도 노인 지원 프로그램"
    docs = mariner_search(question, "TEST_OKMS_V3", "경남")
    for doc in docs:
        logger.info(str(doc))