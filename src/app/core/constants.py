"""
Constants for the RAG chatbot application.
런타임 환경과 무관하게 코드 내에서 변하지 않는 값만 정의합니다.
"""

# ============================================================================
# API Endpoints
# ============================================================================
CHAT_COMPLETIONS_ENDPOINT = "/v1/chat/completions"

# ============================================================================
# Message Roles
# ============================================================================
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

# ============================================================================
# Mariner 쿼리
# ============================================================================
MARINER_SELECT_FIELD_NUM = 16        # SelectSet 필드 수
MARINER_SETPROPS_EXTRA = 100         # setProps 고정 파라미터

# WhereSet 그룹 연산자
# MARINER_WS_OR = 9                    # OR 그룹 시작
# MARINER_WS_AND = 6                   # AND 연결
# MARINER_WS_END = 10                  # 그룹 종료
# MARINER_WS_FILTER = 5                # 필터 조건 시작
# MARINER_WS_NOT = 7                   # NOT 조건
# # WhereSet 검색 모드
# MARINER_WS_BM25 = 2                  # BM25 키워드 검색
# MARINER_WS_VECTOR = 96               # 벡터 유사도 검색
# MARINER_WS_EXACT = 33                # 스크립틀릿 정확 매칭

OP_HASALL = 1 # #Keyword로부터 추출된 모든 단어를 포함한 문서를 가져옴
OP_HASANY = 2 # #Keyword로부터 추출된 단어 중 하나라도 포함된 문서를 
OP_HASANYONE = 3 # #Keyword로부터 추출된 단어 중 하나라도 포함된 문서를 가져옴 (가중치는 하나의 텀만 적용)
OP_SEMIHASANY = 4 # 유사문서 검색
OP_AND = 5 # 두 개의 WhereSet간의 and 연산을 취함
OP_OR = 6 # 두 개의 WhereSet간의 or연산을 취함
OP_NOT = 7 # 두 개의 WhereSet간의 not 연산을 취함
OP_WEIGHTAND = 8 # 검색 결과는 좌측에 필드가 기준이 되고 교집합으로 출현한 문서에 대해서는 가중치를 줌
OP_BRACE_OPEN = 9 # 괄호 열기 연산 (가중치는 하나의 텀만 적용)
OP_BRACE_CLOSE = 10 # 괄호 닫기 연산 
OP_HASALLONE = 11 # Keyword로부터 추출된 모든 단어를 포함한 문서를 가져옴 
OP_TRUNCATION = 12 # 절단 검색 판별을 위한 Operation (has All)Copyright DiQuest Inc. Mariner5 3
OP_RIGHT_TRUNCATION = 64 # 우측의 내용을 절단하여 검색
OP_LEFT_TRUNCATION = 65 # 좌측의 내용을 절단하여 검색
OP_CENTER_TRUNCATION = 66 # 중간의 내용을 절단하여 검색
OP_RIGHT_LEFT_TRUNCATION = 67 # 양측의 내용을 절단하여 검색
EQUIV_SYNONYM = 16 # 특정 필드에 대한 동의어 확장을 지원함
QUASI_SYNONYM = 32 # 특정 필드에 대한 유의어 확장을 지원함
NO_HIGHLIGHT = 128 # 특정 필드에서 추출된 텀을 하이라이팅 하지 않음
OP_PROXIMITY_NEAR = 14 # 특정 필드에 대한 근접 검색(검색어 순서 고려하지 않음) 
OP_PROXIMITY_WITHIN = 15 # 특정 필드에 대한 근접 검색(검색어 순서 고려)
OP_VECTOR_SEARCH = 96 # Keyword로부터 벡터검색을 수행함 (벡터필드만 사용)

# Protocol.GroupBySet
OP_INT_SUMMATION = 33 # 그룹별 Int 값의 합





# 검색 필드 가중치
MARINER_WEIGHT_HIGH: float = 0.7     # 주요 필드 가중치
MARINER_WEIGHT_MED: float = 0.3      # 보조 필드 가중치
MARINER_WEIGHT_LOW: float = 0.1      # 약 가중치 (시군 등)

# ============================================================================
# LLM 처리 제한
# ============================================================================
LLM_MAX_TOTAL_DOC_CHARS = 12000      # RAG 컨텍스트 총 문서 최대 문자
LLM_MAX_DOC_CHUNK_CHARS = 3500       # 문서당 최대 문자
LLM_SYSTEM_MSG_MAX_CHARS = 4000      # 시스템 메시지 절단 기준
LLM_MSG_MAX_CHARS = 3000             # 일반 메시지 절단 기준
LLM_TRUNCATE_HEAD_RATIO: float = 0.7 # 컨텍스트 절단 시 앞부분 비율

# guide_recommend 최종 응답 LLM 호출의 max_tokens 하한.
# 카드 포맷(여러 사업 안내)이 1024 한도에서 잘리는 사례가 잦아 2048로 상향.
# 요청값(chat_request.max_completion_tokens)이 이보다 작거나 None이면 이 값을 사용한다.
GUIDE_RECOMMEND_MAX_TOKENS = 2048

# ============================================================================
# TTS 처리 제한
# ============================================================================
TTS_MAX_CHUNK_CHARS = 220            # TTS 청크 최대 문자
TTS_VOICE_CLEAN_MAX_TOKENS = 1000    # 음성 정제 LLM 최대 토큰

# ============================================================================
# CSV 복지 데이터 점수
# ============================================================================
CSV_WELFARE_BASE_SCORE: float = 0.5
CSV_WELFARE_CENTER_NAME_BONUS: float = 0.2
CSV_WELFARE_CENTER_TYPE_BONUS: float = 0.05
CSV_WELFARE_TEL_NAME_BONUS: float = 0.15
CSV_WELFARE_TEL_TYPE_BONUS: float = 0.1

# ============================================================================
# Logging
# ============================================================================
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s [conv_id=%(conv_id)s] - %(message)s"
LOG_MAX_BYTES = 10_000_000
LOG_BACKUP_COUNT = 5
LOG_ENCODING = "utf-8"

# ============================================================================
# Streaming
# ============================================================================
# 비스트리밍 결과(에러·동기 응답)를 SSE로 재방출할 때 글자당 인위 지연.
# 0.0이면 가능한 빠르게 yield하여 클라이언트에 즉시 흘려보낸다.
# 타이핑 효과가 필요하면 0.005 등 작은 값으로 조정 가능.
STREAM_CHUNK_DELAY = 0.0  # seconds
STREAM_FINISH_MARKER = "[DONE]"

# ============================================================================
# LLM Log Messages
# ============================================================================
LOG_LLM_REQUEST_TIME = "[LLM] Request 시각: %s"
LOG_LLM_RESPONSE_TIME = "[LLM] Response 시각: %s"
LOG_LLM_CLIENT_TIME = "[LLM] 클라이언트 생성시간: %.3f 초"
LOG_LLM_API_TIME = "[LLM] API 호출시간 : %.3f 초"
LOG_LLM_RESPONSE_COMPLETE = "[LLM] 응답 수신완료까지 시간 : %.3f 초"
LOG_LLM_COMPLETE = "[LLM] 완료 | status=%s | content_len=%s"
