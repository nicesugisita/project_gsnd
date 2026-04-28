import jpype
from jpype import JString
from enum import IntEnum
import glob
import os
import logging

logger = logging.getLogger(__name__)

mariner_ip = '10.10.10.102'
mariner_port = '5555'
timeout = 30000  # 60000 → 300000ms (5분)
collection_name = 'GOV_OKMS_V1'
threshold = 0.2
top_n = 5
question = "부모님이 두 분 다 70대이신데 받을 수 있는 혜택이 뭐가 있을까요"

doc_service_id_list = []
doc_service_name_list = []
doc_responsible_ministry_list = []
doc_target_details_list = []
doc_selection_criteria_list = []
doc_benefit_details_list = []
doc_contact_point_list = []
doc_service_type_list = []
doc_life_cycle_list = []
doc_interest_topics_list = []
doc_service_description_list = []
doc_score_list = []
doc_application_period_list = []
doc_service_description_list = []



def __get_jar_files():
    lib_path = '/home/diquest/gsnd_rag_v4/backend/jar_lib' # 마리너5 라이브러리 경로
    jar_files = glob.glob(os.path.join(lib_path, '*.jar'))
    return jar_files

def __start_jvm(jar_files_list):
    # JPype JVM 시작
    # jar 파일들 classpath로 지정해주기
    if not jpype.isJVMStarted():
        jpype.startJVM(
            jpype.getDefaultJVMPath(),
            "-Djava.class.path={classpath}".format(classpath=":".join(jar_files_list)),
            convertStrings=True,
        )
def _run_jpype_jvm():
    jar_files_list = __get_jar_files()
    __start_jvm(jar_files_list)


"""
실제 Mariner5 API 요청 코드
"""
######################################
# JVM 시작 (반드시 Java 객체 사용 전에 실행)
if not jpype.isJVMStarted():
    jpype.startJVM(
        jpype.getDefaultJVMPath(),
        "-Djava.class.path={classpath}".format(classpath=":".join(__get_jar_files())),
        convertStrings=True,
    )

jar_files = __get_jar_files()
print(jar_files)


# m5_client.jar : com/diquest/ir5/client/command/CommandSearchRequest.class
jpkg = jpype.JPackage("com.diquest.ir5.client.command")

command = jpkg.CommandSearchRequest(mariner_ip, int(mariner_port))

# type hint : String, int, int, int, bool
# ip, port, timeout, min_pool, max_pool
# command.setProps(mariner_ip, int(mariner_port), timeout, 100, 100)
command.setProps(mariner_ip, int(mariner_port), timeout, 100, 100)

# query
# com/diquest/ir5/common/msg/protocol/query/Query.class
jpkg = jpype.JPackage("com.diquest.ir5.common.msg.protocol.query")


startTag = ""
endTag = ""

query = jpkg.Query(startTag, endTag)

# top_n 설정
startnum = 0
endnum = int(top_n) -1

# 기본 설정
query.setResult(startnum, endnum) # 몇개의 결과를 가져올지
query.setFrom(collection_name) # 컬렉션 이름
query.setSearch(True)
query.setDebug(False)
query.setPrintQuery(False)
query.setLoggable(True)


# 벡터 색인 필드를 사용한 검색 시, 일정 이상의 유사도를 가진 문서만 출력하도록 하는 설정
# 범위 : -1.0 ~ 1.0 , default : 0.2 (-1.0 으로 설정 시 모든 문서가 출력되므로 주의)
# Type Hint : String Key, String Value
query.setValue("VS_THRESHOLD", str(threshold))

max_top_n = 50
query.setValue("VS_RESULT_SIZE", str(max_top_n)) # default : "20" top N 제한 설정

num = 16  # default 16

# SelectSet(field, option, summarysize) / Type Hint : JavaString, byte, int / char[], byte, int
# GOV_OKMS_V1 컬렉션 필드: 
#                            LIFE_CYCLE, PURPOSE, SIGUN, TEL, YEAR, ORG_NM
doc_service_id_field        = jpkg.SelectSet(JString("SERVICE_ID"),         num, 0)
doc_service_name_field   = jpkg.SelectSet(JString("SERVICE_NAME"),       num, 0)
doc_responsible_ministry_field         = jpkg.SelectSet(JString("RESPONSIBLE_MINISTRY"),    num, 0)
doc_target_details_field           = jpkg.SelectSet(JString("TARGET_DETAILS"),              num, 0)
doc_selection_criteria_field      = jpkg.SelectSet(JString("SELECTION_CRITERIA"),         num, 0)
doc_benefit_details_field          = jpkg.SelectSet(JString("BENEFIT_DETAILS"),             num, 0)
# doc_base_year_field            = jpkg.SelectSet(JString("BASE_YEAR"),               num, 0)
doc_contact_point_field = jpkg.SelectSet(JString("CONTACT_POINT"), num, 0)
doc_service_type_field         = jpkg.SelectSet(JString("SERVICE_TYPE"),            num, 0)
doc_life_cycle_field      = jpkg.SelectSet(JString("LIFE_CYCLE"),         num, 0)
doc_interest_topics_field             = jpkg.SelectSet(JString("INTEREST_TOPICS"),                num, 0)
weight_field              = jpkg.SelectSet(JString("WEIGHT"),             num, 0)
doc_service_description_field         = jpkg.SelectSet(JString("SERVICE_DESCRIPTION"),            num, 0)
doc_application_period_field = jpkg.SelectSet(JString("APPLICATION_PERIOD"), num, 0)

select_set_array = [
    doc_service_id_field,           # index 0
    doc_service_name_field,      # index 1
    doc_responsible_ministry_field,            # index 2
    doc_target_details_field,              # index 3
    doc_selection_criteria_field,         # index 4
    doc_benefit_details_field,             # index 5
    doc_contact_point_field, # index 6
    doc_service_type_field,            # index 7
    doc_life_cycle_field,         # index 8
    doc_interest_topics_field,                # index 9
    weight_field,                 # index 10
    doc_service_description_field,            # index 11
    doc_application_period_field, # index 13
]

# 벡터 색인필드 설정 (SERVICE_DESCRIPTION 필드 기반 벡터 인덱스명 확인 필요)
keyword_string = JString(question)

life_cycle = "노년"

# 공통 기본 조건
where_set_array = [
    jpkg.WhereSet(9),
    jpkg.WhereSet("SERVICE_NAME_KO", 2,  keyword_string, 0.7),
    jpkg.WhereSet(6),
    jpkg.WhereSet("TEXT_CHUNK_KO",    2,  keyword_string, 0.7),
    jpkg.WhereSet(6),
    jpkg.WhereSet("SERVICE_NAME_MI", 2,  keyword_string),
    jpkg.WhereSet(6),
    jpkg.WhereSet("TEXT_CHUNK_MI",    96, keyword_string, 0.3),
    jpkg.WhereSet(10),
]

# 생애주기 조건 스크립틀릿
if life_cycle:
	where_set_array += [
		jpkg.WhereSet(5),
		jpkg.WhereSet("LIFE_CYCLE", 34, life_cycle, 0),
	]


# Query에 설정 적용
query.setSelect(select_set_array)
query.setWhere(where_set_array)

startTag = ""
endTag = ""

queryset = jpkg.QuerySet(1)
queryset.addQuery(query)

order_set_array = []
# order_set_array.append(jpkg.OrderBySet(True, "YEAR", jpype.JByte(97)))
order_set_array.append(jpkg.OrderBySet(True, "WEIGHT", jpype.JByte(97)))
# order_set_array.append(jpkg.OrderBySet(False, "WEIGHT")) # 검색 정확도 순 정렬




query.setOrderby(order_set_array)
logger.debug(f"마리너 요청 정보 - 서버: {mariner_ip}:{mariner_port}, 컬렉션: {collection_name}, Top N: {top_n}, Threshold: {threshold}")

"""
60003 : connection failed, 접속 불가 관련 (방화벽, port 오픈 등 확인)
60004 : timeout 관련 에러코드, setProps 에서 설정한 시간이 지날때까지 검색 결과 응답이 오지 않은 경우
60005 : 전송 중 network IOException이 발생한 경우
60006 : QuerySet / ResultSet 객체의 transmit error
60011 : 검색서버 내의 Exception 발생으로 socket이 close 된 경우
"""

# request 응답 가져오기
try:
    ret = command.request(queryset)
except Exception as e:
    logger.error(f"[예외 발생] {type(e).__name__}: {e}")
    ret = -1
logger.debug(f"Mariner 요청 결과 코드: {ret}")

# 에러 코드별 메시지 처리
if (ret < 0) :
	if str(ret) == "-60003":
		msg = "접속 실패: 방화벽 설정, 포트 오픈 등 네트워크 설정을 확인해주세요."
	elif str(ret) == "-60004":
		msg = "TimeOut으로 Mariner 데이터 검색에 실패하였습니다. 다시 시도해주세요."
	elif str(ret) == "-60005":
		msg = "데이터 전송 중 네트워크 IOException이 발생하였습니다. 네트워크 상태를 확인 후 다시 시도해주세요."
	elif str(ret) == "-60006":
		msg = "QuerySet / ResultSet 객체의 전송 중 오류가 발생하였습니다."
	elif str(ret) == "-60011":
		msg = "검색 서버 내부 Exception으로 인해 소켓 연결이 종료되었습니다. 관리자에게 문의해주세요."
	else:
		msg = f"알 수 없는 오류가 발생하였습니다. 에러 코드: {ret}"

	logger.error(f"에러 코드: {ret}, 메시지: {msg}")
	raise RuntimeError(msg)

resultSet = command.getResultSet()
if resultSet is None:
	logger.error("Mariner 서버에서 결과를 받지 못했습니다. 서버 상태, 파라미터, 로그를 확인하세요.")
else:
	result = resultSet.getResult(0)
	result_size = result.getRealSize()
	if not result_size == 0:
		for i in range(result_size):
			doc_service_id_list.append(str(result.getResult(i, 0)))           # SERVICE_ID
			doc_service_name_list.append(str(result.getResult(i, 1)))         # SERVICE_NAME
			doc_responsible_ministry_list.append(str(result.getResult(i, 2))) # RESPONSIBLE_MINISTRY
			doc_target_details_list.append(str(result.getResult(i, 3)))       # TARGET_DETAILS
			doc_selection_criteria_list.append(str(result.getResult(i, 4)))   # SELECTION_CRITERIA
			doc_benefit_details_list.append(str(result.getResult(i, 5)))      # BENEFIT_DETAILS
			doc_contact_point_list.append(str(result.getResult(i, 6)))        # CONTACT_POINT
			doc_service_type_list.append(str(result.getResult(i, 7)))         # SERVICE_TYPE
			doc_life_cycle_list.append(str(result.getResult(i, 8)))           # LIFE_CYCLE
			doc_interest_topics_list.append(str(result.getResult(i, 9)))      # INTEREST_TOPICS
			score_raw = str(result.getResult(i, 10))                          # WEIGHT
			doc_score = float(score_raw) * 0.0001 if score_raw.strip() else 0.0
			doc_score_list.append(str(doc_score))
			doc_service_description_list.append(str(result.getResult(i, 11))) # SERVICE_DESCRIPTION
			doc_application_period_list.append(str(result.getResult(i, 12))) # APPLICATION_PERIOD

logger.debug(f"Mariner 검색 결과 수: {len(doc_service_id_list)}")
for i in range(len(doc_service_id_list)):
	print(
		f"서비스 설명={doc_service_description_list[i]}\n",
		f"유사도={doc_score_list[i]}\n",
		f"==========================================="
	)
	logger.debug(
		f"[{i}] 서비스ID={doc_service_id_list[i]}, 서비스명={doc_service_name_list[i]}, "
		f"대상 세부사항={doc_service_description_list[i]}, 유사도={doc_score_list[i]}"
	)
