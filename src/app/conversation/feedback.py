"""
Message feedback persistence service.

Stores copy/like/dislike actions per assistant message.
"""

import logging
from datetime import datetime
from typing import Optional

import mysql.connector
from mysql.connector import Error as MySQLError

from app.core.config import Config

logger = logging.getLogger(__name__)

FEEDBACK_TABLE = "gsnd_message_feedback"


class MessageFeedbackService:
    """Service for storing assistant message feedback in MariaDB."""

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
                autocommit=True,
            )
        except MySQLError as e:
            logger.error(f"Database connection error: {e}")
            raise

    def upsert_feedback(
        self,
        user_id: Optional[str],
        conv_id: str,
        message_index: int,
        action: str,
    ) -> bool:
        """Store feedback action for one assistant message."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            if action == "copy":
                query = f"""
                    INSERT INTO {FEEDBACK_TABLE}
                        (user_id, conv_id, message_index, copied_count, updated_at)
                    VALUES (%s, %s, %s, 1, %s)
                    ON DUPLICATE KEY UPDATE
                        copied_count = copied_count + 1,
                        updated_at = VALUES(updated_at)
                """
                cursor.execute(query, (user_id, conv_id, message_index, datetime.now()))
            else:
                reaction = None
                if action == "like":
                    reaction = "like"
                elif action == "dislike":
                    reaction = "dislike"

                query = f"""
                    INSERT INTO {FEEDBACK_TABLE}
                        (user_id, conv_id, message_index, reaction, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        reaction = VALUES(reaction),
                        updated_at = VALUES(updated_at)
                """
                cursor.execute(query, (user_id, conv_id, message_index, reaction, datetime.now()))

            cursor.close()
            return True
        except MySQLError as e:
            logger.error(f"Error upserting message feedback: {e}")
            return False
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
                    autocommit=True,
                )
                close_conn = True
            else:
                conn = connection

            cursor = conn.cursor()
            create_table_query = f"""
                CREATE TABLE IF NOT EXISTS {FEEDBACK_TABLE} (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id VARCHAR(255) NULL,
                    conv_id VARCHAR(255) NOT NULL,
                    message_index INT NOT NULL,
                    reaction VARCHAR(16) NULL,
                    copied_count INT NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uniq_feedback (conv_id, message_index),
                    INDEX idx_user_id (user_id),
                    INDEX idx_conv_id (conv_id),
                    INDEX idx_updated_at (updated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            cursor.execute(create_table_query)

            # Backward compatibility for already-created schema
            try:
                cursor.execute(f"ALTER TABLE {FEEDBACK_TABLE} MODIFY COLUMN user_id VARCHAR(255) NULL")
            except MySQLError:
                pass

            try:
                cursor.execute(f"ALTER TABLE {FEEDBACK_TABLE} DROP INDEX uniq_feedback")
            except MySQLError:
                pass

            try:
                cursor.execute(
                    f"ALTER TABLE {FEEDBACK_TABLE} ADD UNIQUE KEY uniq_feedback (conv_id, message_index)"
                )
            except MySQLError:
                pass

            try:
                cursor.execute(f"ALTER TABLE {FEEDBACK_TABLE} ADD INDEX idx_user_id (user_id)")
            except MySQLError:
                pass

            cursor.close()

            logger.info(f"{FEEDBACK_TABLE} table created or already exists")
            return True
        except MySQLError as e:
            logger.error(f"Error creating message feedback table: {e}")
            return False
        finally:
            if close_conn and conn:
                conn.close()


_message_feedback_service: Optional[MessageFeedbackService] = None


def get_message_feedback_service(config: Config = None) -> MessageFeedbackService:
    global _message_feedback_service
    if _message_feedback_service is None:
        _message_feedback_service = MessageFeedbackService(config)
        MessageFeedbackService.create_tables()
    return _message_feedback_service
