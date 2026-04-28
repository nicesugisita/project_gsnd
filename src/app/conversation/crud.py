"""
Conversation management service.

Manages multiple conversation sessions for users.
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import mysql.connector
from mysql.connector import Error as MySQLError

from app.core.config import Config

logger = logging.getLogger(__name__)

CONVERSATION_TABLE = "gsnd_conversations"
HISTORY_TABLE = "gsnd_chat_history"
ANONYMOUS_HISTORY_TABLE = "gsnd_chat_history_anonymous"


class ConversationService:
    def update_conversation_timestamp(self, conv_id: str) -> bool:
        """Update only the updated_at timestamp of a conversation."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            query = f"""
                UPDATE {CONVERSATION_TABLE}
                SET updated_at = %s
                WHERE id = %s
            """
            cursor.execute(query, (datetime.now(), conv_id))
            cursor.close()
            return True
        except Exception as e:
            logger.error(f"Error updating conversation timestamp: {e}")
            return False
        finally:
            if conn:
                conn.close()

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

    def create_conversation(self, user_id: str, title: str = Config.DEFAULT_CONVERSATION_TITLE) -> str:
        """Create a new conversation session."""
        conv_id = str(uuid.uuid4())
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            query = f"""
                INSERT INTO {CONVERSATION_TABLE} (id, user_id, title, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
            """
            now = datetime.now()
            cursor.execute(query, (conv_id, user_id, title, now, now))
            cursor.close()
            
            logger.info(f"Created conversation: {conv_id} for user: {user_id}")
            return conv_id
        except MySQLError as e:
            logger.error(f"Error creating conversation: {e}")
            raise
        finally:
            if conn:
                conn.close()

    def get_conversations(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Get all conversations for a user."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = f"""
                SELECT id, title, created_at, updated_at
                FROM {CONVERSATION_TABLE}
                WHERE user_id = %s
                ORDER BY updated_at DESC
                LIMIT %s
            """
            cursor.execute(query, (user_id, limit))
            results = cursor.fetchall()
            cursor.close()
            
            # Convert datetime to string and rename 'id' to 'conv_id'
            for row in results:
                if 'id' in row:
                    row['conv_id'] = row.pop('id')
                if 'created_at' in row and row['created_at']:
                    row['created_at'] = row['created_at'].isoformat()
                if 'updated_at' in row and row['updated_at']:
                    row['updated_at'] = row['updated_at'].isoformat()
            
            return results
        except MySQLError as e:
            logger.error(f"Error fetching conversations: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def delete_conversation(self, conv_id: str, user_id: str) -> bool:
        """Delete a conversation and its messages."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # Verify ownership before deleting
            verify_query = f"""
                SELECT id FROM {CONVERSATION_TABLE}
                WHERE id = %s AND user_id = %s
            """
            cursor.execute(verify_query, (conv_id, user_id))
            if not cursor.fetchone():
                cursor.close()
                return False
            
            # Delete chat history first
            history_delete_query = f"DELETE FROM {HISTORY_TABLE} WHERE conv_id = %s"
            cursor.execute(history_delete_query, (conv_id,))

            anonymous_history_delete_query = f"DELETE FROM {ANONYMOUS_HISTORY_TABLE} WHERE conv_id = %s"
            cursor.execute(anonymous_history_delete_query, (conv_id,))
            
            # Delete conversation
            delete_query = f"""
                DELETE FROM {CONVERSATION_TABLE}
                WHERE id = %s
            """
            cursor.execute(delete_query, (conv_id,))
            cursor.close()
            
            logger.info(f"Deleted conversation and history: {conv_id}")
            return True
        except MySQLError as e:
            logger.error(f"Error deleting conversation: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def get_conversation_messages(self, conv_id: str) -> List[Dict[str, Any]]:
        """Get all messages in a conversation."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor(dictionary=True)
            
            query = f"""
                SELECT messages_json
                FROM {HISTORY_TABLE}
                WHERE conv_id = %s
                ORDER BY updated_at DESC
                LIMIT 1
            """
            cursor.execute(query, (conv_id,))
            row = cursor.fetchone()
            cursor.close()
            
            if not row or not row.get('messages_json'):
                return []
            
            import json
            return json.loads(row['messages_json'])
        except MySQLError as e:
            logger.error(f"Error fetching conversation messages: {e}")
            return []
        finally:
            if conn:
                conn.close()

    @staticmethod
    def create_tables(connection=None) -> bool:
        """Create conversations table."""
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
                CREATE TABLE IF NOT EXISTS {CONVERSATION_TABLE} (
                    id VARCHAR(255) PRIMARY KEY,
                    user_id VARCHAR(255) NOT NULL,
                    title VARCHAR(500) NOT NULL DEFAULT '새로운 대화',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_user_id (user_id),
                    INDEX idx_updated_at (updated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            cursor.execute(create_table_query)
            cursor.close()
            
            logger.info(f"{CONVERSATION_TABLE} table created or already exists")
            return True
        except MySQLError as e:
            logger.error(f"Error creating conversations table: {e}")
            return False
        finally:
            if close_conn and conn:
                conn.close()


_conversation_service: Optional[ConversationService] = None


def get_conversation_service(config: Config = None) -> ConversationService:
    """Get or create the global ConversationService instance."""
    global _conversation_service
    if _conversation_service is None:
        _conversation_service = ConversationService(config)
        ConversationService.create_tables()
    return _conversation_service
