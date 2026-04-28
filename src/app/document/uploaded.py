"""
Uploaded document persistence service.

Stores uploaded document metadata for document-summary workflow.
"""

import logging
from typing import Optional, List, Dict, Any

import mysql.connector
from mysql.connector import Error as MySQLError

from app.core.config import Config

logger = logging.getLogger(__name__)

UPLOADED_DOCUMENTS_TABLE = "gsnd_uploaded_documents"
ANONYMOUS_USER_MARKER = "__anonymous__"
DEFAULT_CONV_ID = "document-summary"


class UploadedDocumentService:
    """Service for persisting uploaded document metadata."""

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

    def save_uploaded_document(
        self,
        conv_id: Optional[str],
        user_id: Optional[str],
        name: str,
        path: str,
    ) -> bool:
        normalized_conv_id = (conv_id or DEFAULT_CONV_ID)
        normalized_user_id = (user_id or "").strip() or ANONYMOUS_USER_MARKER

        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            query = f"""
                INSERT INTO {UPLOADED_DOCUMENTS_TABLE} (conv_id, user_id, name, path)
                VALUES (%s, %s, %s, %s)
            """
            cursor.execute(query, (normalized_conv_id, normalized_user_id, name, path))
            cursor.close()
            return True
        except MySQLError as e:
            logger.error(f"Error saving uploaded document metadata: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def get_uploaded_documents(
        self,
        conv_id: Optional[str],
        user_id: Optional[str],
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        normalized_conv_id = (conv_id or "").strip() or DEFAULT_CONV_ID
        normalized_user_id = (user_id or "").strip() or ANONYMOUS_USER_MARKER
        safe_limit = max(1, min(int(limit or 5), 20))

        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor(dictionary=True)

            query = f"""
                SELECT id, conv_id, user_id, name, path, created_at
                FROM {UPLOADED_DOCUMENTS_TABLE}
                WHERE conv_id = %s AND user_id = %s
                ORDER BY id DESC
                LIMIT %s
            """
            cursor.execute(query, (normalized_conv_id, normalized_user_id, safe_limit))
            rows = cursor.fetchall() or []
            cursor.close()
            return rows
        except MySQLError as e:
            logger.error(f"Error fetching uploaded documents: {e}")
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
                    autocommit=True,
                )
                close_conn = True
            else:
                conn = connection

            cursor = conn.cursor()
            create_table_query = f"""
                CREATE TABLE IF NOT EXISTS {UPLOADED_DOCUMENTS_TABLE} (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    conv_id VARCHAR(255) NOT NULL,
                    user_id VARCHAR(255) NOT NULL,
                    name VARCHAR(500) NOT NULL,
                    path TEXT NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            cursor.execute(create_table_query)
            cursor.close()
            logger.info(f"{UPLOADED_DOCUMENTS_TABLE} table created or already exists")
            return True
        except MySQLError as e:
            logger.error(f"Error creating uploaded documents table: {e}")
            return False
        finally:
            if close_conn and conn:
                conn.close()


_uploaded_document_service: Optional[UploadedDocumentService] = None


def get_uploaded_document_service(config: Config = None) -> UploadedDocumentService:
    global _uploaded_document_service
    if _uploaded_document_service is None:
        _uploaded_document_service = UploadedDocumentService(config)
        UploadedDocumentService.create_tables()
    return _uploaded_document_service
