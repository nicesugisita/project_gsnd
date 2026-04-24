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
MARINER_WS_OR = 9                    # OR 그룹 시작
MARINER_WS_AND = 6                   # AND 연결
MARINER_WS_END = 10                  # 그룹 종료
MARINER_WS_FILTER = 5                # 필터 조건 시작

# WhereSet 검색 모드
MARINER_WS_BM25 = 2                  # BM25 키워드 검색
MARINER_WS_VECTOR = 96               # 벡터 유사도 검색
MARINER_WS_EXACT = 33                # 스크립틀릿 정확 매칭

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
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
LOG_MAX_BYTES = 10_000_000
LOG_BACKUP_COUNT = 5
LOG_ENCODING = "utf-8"

# ============================================================================
# Streaming
# ============================================================================
STREAM_CHUNK_DELAY = 0.01  # seconds
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
