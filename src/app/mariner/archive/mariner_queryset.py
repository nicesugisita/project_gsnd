################################
## 수정일 : 2026-03-09
## 마리너 검색시 where 조건에 USER_ID와 CONV_ID 추가하여 검색 정확도 향상
################################

import jpype
from jpype import JString
import glob
import os
import logging
import re
from app.utils.prompt_loader import load_collection_category_prompt
from app.core.config import Config

logger = logging.getLogger(__name__)

mariner_ip = '10.10.10.102'
mariner_port = '5555'
timeout = 60000
threshold = 0.2
top_n = Config.RAG_NUM_REFERENCED_DOCS

def get_collection_name_from_prompt():
    prompt_text = load_collection_category_prompt()
    match = re.search(r'(GSND_V2_TOTAL)', prompt_text)
    if match:
        return match.group(1)
    else:
        return 'GSND_V2_TOTAL'  # 기본값

def mariner_search(question, collection_name, user_id, conv_id):
    if not user_id or not conv_id:
        logger.warning("user_id 또는 conv_id가 없어 문서 검색을 수행하지 않습니다.")
        return []

    doc_title_list = []
    doc_text_chunk_list = []
    doc_score_list = []
    doc_chunk_id_list = []
    doc_user_id_list = []
    doc_conv_id_list = []

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
    max_top_n = 50
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

    doc_title_field = jpkg.SelectSet(JString("NAME"), num, 0)
    doc_text_chunk_field = jpkg.SelectSet(JString("CHUNK_PATH"), num, 0)
    doc_chunk_id_field = jpkg.SelectSet(JString("CHUNK_ID"), num, 0)
    weight_field = jpkg.SelectSet(JString("WEIGHT"), num, 0)
    doc_user_id_field = jpkg.SelectSet(JString("USER_ID"), num, 0) ## 추가
    doc_conv_id_field = jpkg.SelectSet(JString("CONV_ID"), num, 0) ## 추가

    select_set_array = [doc_chunk_id_field, doc_title_field, doc_text_chunk_field, weight_field, doc_user_id_field, doc_conv_id_field] ## 추가
    query.setSelect(select_set_array)

    keyword_string = JString(question)
 
    where_set_array = [
        jpkg.WhereSet("TEXT_CHUNK_KO", 2, keyword_string, 0.01),
        jpkg.WhereSet(6),
        jpkg.WhereSet("TEXT_CHUNK_MI", 96, keyword_string, 0.99)
    ]
    query.setWhere(where_set_array)

    queryset = jpkg.QuerySet(1)
    queryset.addQuery(query)
    # print(queryset)

    order_set_array = []
    order_set_array.append(jpkg.OrderBySet(False, "WEIGHT")) # 검색 정확도 순 정렬
    # query.setSelect(select_set_array)
    # query.setWhere(where_set_array)
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
    for i in range(result_size):
        result_user_id = str(result.getResult(i, 4))
        result_conv_id = str(result.getResult(i, 5))

        if result_user_id != str(user_id) or result_conv_id != str(conv_id):
            continue

        doc_chunk_id_list.append(str(result.getResult(i, 0)))
        doc_title_list.append(str(result.getResult(i, 1)))
        doc_text_chunk_list.append(str(result.getResult(i, 2)))
        # doc_score = round(float(str(result.getResult(i, 3))) * 0.0001, 4)
        # doc_score = float(str(result.getResult(i, 3)))
        doc_score = str(result.getResult(i, 3))
        doc_user_id_list.append(str(result.getResult(i, 4)))
        doc_conv_id_list.append(str(result.getResult(i, 5)))
        doc_score_list.append(str(doc_score))

    # print(f"=== Mariner 검색 결과: {result_size}개 ===")
    print(doc_score_list)

    # 원하는 형태로 반환
    return [
        {
            "chunk_id": doc_chunk_id_list[i],
            "title": doc_title_list[i],
            "text_chunk": doc_text_chunk_list[i],
            "score": doc_score_list[i],
            "user_id": str(user_id),
            "conv_id": str(conv_id),
        }
        for i in range(min(len(doc_chunk_id_list), int(top_n)))
    ]

# 사용 예시
if __name__ == "__main__":
    # collection_name = get_collection_name_from_prompt()
    question = "신청방법 알려줭"
    # docs = mariner_search(question, collection_name)
    docs = mariner_search(question, "GSND_UPLOADED_FILE_V1", "__anonymous__", "f5f996ae-acc1-436d-8e6e-438f5182fef2")
    for doc in docs:
        print(doc)