"""
Document summary persistence service.

Stores document summary, metadata, and Q&A for uploaded documents.
"""

import logging
from typing import Optional, List, Dict, Any

import mysql.connector
from mysql.connector import Error as MySQLError

from core.config import Config

logger = logging.getLogger(__name__)

DOCUMENT_SUMMARY_TABLE = "gsnd_document_summary"


class DocumentSummaryService:
    """Service for persisting document summary and Q&A."""

    def update_qa_json(self, doc_id: str, qa_json: str) -> bool:
        """Update qa_json for a document summary row by doc_id."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            query = f"""
                UPDATE {DOCUMENT_SUMMARY_TABLE}
                SET qa_json = %s
                WHERE doc_id = %s
            """
            cursor.execute(query, (qa_json, doc_id))
            cursor.close()
            return True
        except MySQLError as e:
            logger.error(f"Error updating qa_json: {e}")
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
                autocommit=True,
            )
        except MySQLError as e:
            logger.error(f"Database connection error: {e}")
            raise

    def save_document_summary(
        self,
        doc_id: str,
        user_id: Optional[str],
        filename: str,
        summary: str,
        upload_time: str,
    ) -> bool:
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            query = f"""
                INSERT INTO {DOCUMENT_SUMMARY_TABLE} (doc_id, user_id, filename, summary, upload_time)
                VALUES (%s, %s, %s, %s, %s)
            """
            cursor.execute(query, (doc_id, user_id, filename, summary, upload_time))
            cursor.close()
            return True
        except MySQLError as e:
            logger.error(f"Error saving document summary: {e}")
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
                CREATE TABLE IF NOT EXISTS {DOCUMENT_SUMMARY_TABLE} (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    doc_id VARCHAR(255) NOT NULL,
                    user_id VARCHAR(255),
                    filename VARCHAR(500) NOT NULL,
                    summary TEXT NOT NULL,
                    upload_time DATETIME NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            cursor.execute(create_table_query)
            cursor.close()
            logger.info(f"{DOCUMENT_SUMMARY_TABLE} table created or already exists")
            return True
        except MySQLError as e:
            logger.error(f"Error creating document summary table: {e}")
            return False
        finally:
            if close_conn and conn:
                conn.close()

_document_summary_service: Optional[DocumentSummaryService] = None
