"""
Chat history persistence service.

Stores and retrieves conversation history by user_id and conv_id.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import mysql.connector
from mysql.connector import Error as MySQLError

from app.core.config import Config

from app.core.constants import ROLE_ASSISTANT
logger = logging.getLogger(__name__)

HISTORY_TABLE = "gsnd_chat_history"
ANONYMOUS_HISTORY_TABLE = "gsnd_chat_history_anonymous"
ANONYMOUS_USER_MARKER = "__anonymous__"


class ChatHistoryService:
    """Service for managing chat history in MariaDB."""

    def __init__(self, config: Config = None):
        if config is None:
            config = Config
        self.config = config
        self.db_host = config.DB_HOST
        self.db_user = config.DB_USER
        self.db_password = config.DB_PASSWORD
        self.db_name = config.DB_NAME
        self.db_port = config.DB_PORT

    def _get_connection(self):
        try:
            return mysql.connector.connect(
                host=self.db_host,
                user=self.db_user,
                password=self.db_password,
                database=self.db_name,
                port=self.db_port,
                autocommit=True
            )
        except MySQLError as e:
            logger.error(f"Database connection error: {e}")
            raise

    def upsert_history(
        self,
        user_id: Optional[str],
        conv_id: str,
        messages: List[Dict[str, Any]],
        overwrite: bool = False  # 옵션 추가: 기본값은 기존처럼 합치기(False)
    ) -> None:
        """Save chat history for a conversation."""
        if not conv_id:
            return

        normalized_user_id = user_id.strip() if isinstance(user_id, str) else ""
        is_anonymous = (not normalized_user_id) or normalized_user_id == ANONYMOUS_USER_MARKER
        target_table = ANONYMOUS_HISTORY_TABLE if is_anonymous else HISTORY_TABLE
        persist_user_id = None if is_anonymous else normalized_user_id

        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            # 1. 덮어쓰기 모드가 아닐 때만 기존 메시지 조회
            existing_messages = []
            if not overwrite:
                try:
                    get_query = f"SELECT messages_json FROM {target_table} WHERE conv_id = %s" if is_anonymous else f"SELECT messages_json FROM {target_table} WHERE user_id = %s AND conv_id = %s"
                    if is_anonymous:
                        cursor.execute(get_query, (conv_id,))
                    else:
                        cursor.execute(get_query, (persist_user_id, conv_id))
                    row = cursor.fetchone()
                    if row and row[0]:
                        existing_messages = json.loads(row[0])
                except Exception as e:
                    logger.warning(f"기존 대화내역 조회 실패: {e}")

            # 2. 메시지 결합 결정
            if overwrite:
                # 덮어쓰기 모드면 전달받은 리스트 그대로 저장
                final_messages = messages
            else:
                # 일반 모드면 기존 내역 + 새 내역
                final_messages = list(existing_messages) + list(messages)

            payload = json.dumps(final_messages, ensure_ascii=False)

            # 3. DB 저장 (ON DUPLICATE KEY UPDATE로 덮어쓰기 실행)
            now = datetime.now()
            if is_anonymous:
                query = f"""
                    INSERT INTO {target_table} (conv_id, messages_json, updated_at)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        messages_json = VALUES(messages_json),
                        updated_at = VALUES(updated_at)
                """
                cursor.execute(query, (conv_id, payload, now))
            else:
                query = f"""
                    INSERT INTO {target_table} (user_id, conv_id, messages_json, updated_at)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        messages_json = VALUES(messages_json),
                        updated_at = VALUES(updated_at)
                """
                cursor.execute(query, (persist_user_id, conv_id, payload, now))
            
            cursor.close()
        except Exception as e:
            logger.error(f"Error upserting chat history: {e}")
        finally:
            if conn:
                conn.close()

    def _postprocess_loaded_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for msg in messages:
            if msg.get("role") == ROLE_ASSISTANT:
                if "action" not in msg:
                    msg["action"] = {
                        "type": Config.ASSISTANT_ACTION_FEEDBACK,
                        "options": Config.ASSISTANT_ACTION_FEEDBACK_OPTIONS,
                    }
        return messages

    def get_history(
        self, conv_id: str, user_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get chat history for a conversation.

        user_id:
            None — 비로그인 canonical(conv_id) 행 및 익명 테이블을 먼저 시도한 뒤, 없으면 메인·익명 UNION의 updated_at 최신 1행.
            그 외 — 해당 user_id(또는 익명 마커)만 조회한다.
        """
        if not conv_id:
            return []

        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            row = None

            if user_id is None:
                # 프론트가 user_id/query를 안 보내도 채팅(비로그인)과 같은 conv_id 소유 행을 우선 사용한다.
                guest_scoped = conv_id
                cursor.execute(
                    f"""
                    SELECT messages_json FROM {HISTORY_TABLE}
                    WHERE conv_id = %s AND user_id = %s
                    """,
                    (conv_id, guest_scoped),
                )
                row = cursor.fetchone()
                # 하위호환: 과거 nologin{conv_id} 데이터 fallback
                if not row or not row[0]:
                    old_guest_scoped = f"nologin{conv_id}"
                    cursor.execute(
                        f"""
                        SELECT messages_json FROM {HISTORY_TABLE}
                        WHERE conv_id = %s AND user_id = %s
                        """,
                        (conv_id, old_guest_scoped),
                    )
                    row = cursor.fetchone()
                if not row or not row[0]:
                    cursor.execute(
                        f"SELECT messages_json FROM {ANONYMOUS_HISTORY_TABLE} WHERE conv_id = %s",
                        (conv_id,),
                    )
                    row = cursor.fetchone()
                if row and row[0]:
                    messages = json.loads(row[0])
                    cursor.close()
                    return self._postprocess_loaded_messages(messages)

                query = f"""
                    SELECT messages_json FROM (
                        SELECT messages_json, updated_at FROM {HISTORY_TABLE}
                        WHERE conv_id = %s
                        UNION ALL
                        SELECT messages_json, updated_at FROM {ANONYMOUS_HISTORY_TABLE}
                        WHERE conv_id = %s
                    ) AS all_histories
                    ORDER BY updated_at DESC
                    LIMIT 1
                """
                cursor.execute(query, (conv_id, conv_id))
                row = cursor.fetchone()
            else:
                scope = user_id.strip() if isinstance(user_id, str) and user_id.strip() else None
                if not scope:
                    cursor.close()
                    return []

                guest_scoped = conv_id
                if scope == ANONYMOUS_USER_MARKER:
                    cursor.execute(
                        f"SELECT messages_json FROM {ANONYMOUS_HISTORY_TABLE} WHERE conv_id = %s",
                        (conv_id,),
                    )
                    row = cursor.fetchone()
                else:
                    cursor.execute(
                        f"""
                        SELECT messages_json FROM {HISTORY_TABLE}
                        WHERE conv_id = %s AND user_id = %s
                        """,
                        (conv_id, scope),
                    )
                    row = cursor.fetchone()
                    if (not row or not row[0]) and scope == guest_scoped:
                        # 하위호환: 과거 nologin{conv_id} 데이터 fallback
                        old_guest_scoped = f"nologin{conv_id}"
                        cursor.execute(
                            f"""
                            SELECT messages_json FROM {HISTORY_TABLE}
                            WHERE conv_id = %s AND user_id = %s
                            """,
                            (conv_id, old_guest_scoped),
                        )
                        row = cursor.fetchone()
                    if (not row or not row[0]) and scope == guest_scoped:
                        cursor.execute(
                            f"SELECT messages_json FROM {ANONYMOUS_HISTORY_TABLE} WHERE conv_id = %s",
                            (conv_id,),
                        )
                        row = cursor.fetchone()

            cursor.close()

            if not row or not row[0]:
                return []

            messages = json.loads(row[0])
            return self._postprocess_loaded_messages(messages)

        except MySQLError as e:
            logger.error(f"Error fetching chat history: {e}")
            return []
        finally:
            if conn:
                conn.close()

    @staticmethod
    def create_tables(connection=None) -> bool:
        conn = None
        close_conn = False
        try:
            if connection is None:
                config = Config
                conn = mysql.connector.connect(
                    host=config.DB_HOST,
                    user=config.DB_USER,
                    password=config.DB_PASSWORD,
                    database=config.DB_NAME,
                    port=config.DB_PORT,
                    autocommit=True
                )
                close_conn = True
            else:
                conn = connection

            cursor = conn.cursor()
            create_table_query = f"""
                CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id VARCHAR(255) NOT NULL,
                    conv_id VARCHAR(255) NOT NULL,
                    messages_json LONGTEXT NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_user_conv (user_id, conv_id),
                    INDEX idx_user_id (user_id),
                    INDEX idx_updated_at (updated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            cursor.execute(create_table_query)

            create_anonymous_table_query = f"""
                CREATE TABLE IF NOT EXISTS {ANONYMOUS_HISTORY_TABLE} (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    conv_id VARCHAR(255) NOT NULL,
                    messages_json LONGTEXT NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_conv (conv_id),
                    INDEX idx_updated_at (updated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            cursor.execute(create_anonymous_table_query)
            cursor.close()

            logger.info(f"{HISTORY_TABLE}, {ANONYMOUS_HISTORY_TABLE} tables created or already exist")
            return True
        except MySQLError as e:
            logger.error(f"Error creating chat history table: {e}")
            return False
        finally:
            if close_conn and conn:
                conn.close()


_chat_history_service: Optional[ChatHistoryService] = None


def get_chat_history_service(config: Config = None) -> ChatHistoryService:
    global _chat_history_service
    if _chat_history_service is None:
        _chat_history_service = ChatHistoryService(config)
        ChatHistoryService.create_tables()
    return _chat_history_service
