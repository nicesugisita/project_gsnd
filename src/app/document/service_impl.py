"""
Document management service.

Handles document retrieval and file download operations from the dataset.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import mysql.connector
from mysql.connector import Error as MySQLError

from app.core.config import Config

logger = logging.getLogger(__name__)

class DocumentService:
    """Service for managing documents from the dataset."""

    def __init__(self, config: Config = None):
        if config is None:
            config = Config
        self.config = config
        self.db_host = config.DB_HOST
        self.db_user = config.DB_USER
        self.db_password = config.DB_PASSWORD
        self.db_name = config.DB_NAME
        self.db_port = config.DB_PORT
        self.okms2_db_name = config.OKMS2_DB_NAME
        self.okms_doc_table = config.OKMS_DOC_TABLE
        self.okms_view_table = config.OKMS_VIEW_TABLE
        self.allowed_base_dirs = config.ALLOWED_BASE_DIRS
        self.base_path_aliases = config.BASE_PATH_ALIASES

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

    def _get_okms2_connection(self):
        try:
            return mysql.connector.connect(
                host=self.db_host,
                user=self.db_user,
                password=self.db_password,
                database=self.okms2_db_name,
                port=self.db_port,
                autocommit=True
            )
        except MySQLError as e:
            logger.error(f"OKMS2 database connection error: {e}")
            raise

    def get_document_by_name(self, doc_name: str) -> Optional[Dict[str, Any]]:
        """
        Get document information by name from okms2.VIEW_OKMS_DOC.

        Args:
            doc_name: Document name to search for (ORG_NM)

        Returns:
            Dictionary with id, name, and path, or None if not found
        """
        conn = None
        try:
            conn = self._get_okms2_connection()
            cursor = conn.cursor(dictionary=True)

            query = f"""
                SELECT ORG_NM, UUID_PATH
                FROM {self.okms_doc_table}
                WHERE ORG_NM = %s
                LIMIT 1
            """
            cursor.execute(query, (doc_name,))
            result = cursor.fetchone()
            cursor.close()

            if result:
                return {
                    'id': None,
                    'name': result.get('ORG_NM', ''),
                    'path': result.get('UUID_PATH', '')
                }
            return None
        except MySQLError as e:
            logger.error(f"Error fetching document: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def get_document_by_id(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """
        VIEW_OKMS_DOC에는 ID 컬럼이 없으므로 항상 None을 반환합니다.
        doc_name 기반 조회(get_document_by_name)를 사용하세요.
        """
        return None

    def get_okms_document_by_name(self, org_nm: str) -> Optional[Dict[str, Any]]:
        """
        okms2.VIEW_WLF_SRVC 테이블에서 ORG_NM으로 문서 경로를 조회합니다.

        Args:
            org_nm: ORG_NM 컬럼 값 (파일명)

        Returns:
            Dictionary with name and path, or None if not found
        """
        conn = None
        try:
            conn = mysql.connector.connect(
                host=self.db_host,
                user=self.db_user,
                password=self.db_password,
                database=self.okms2_db_name,
                port=self.db_port,
                autocommit=True
            )
            cursor = conn.cursor(dictionary=True)
            query = f"""
                SELECT ORG_NM, PATH
                FROM {self.okms_view_table}
                WHERE ORG_NM = %s
                LIMIT 1
            """
            cursor.execute(query, (org_nm,))
            result = cursor.fetchone()
            cursor.close()

            if result:
                return {
                    'id': None,
                    'name': result.get('ORG_NM', ''),
                    'path': result.get('PATH', '')
                }
            return None
        except MySQLError as e:
            logger.error(f"Error fetching OKMS document: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def get_documents_by_names(self, doc_names: List[str]) -> List[Dict[str, Any]]:
        """
        Get multiple documents by names from okms2.VIEW_OKMS_DOC.

        Args:
            doc_names: List of document names (ORG_NM)

        Returns:
            List of document information dictionaries
        """
        if not doc_names:
            return []

        conn = None
        try:
            conn = self._get_okms2_connection()
            cursor = conn.cursor(dictionary=True)

            placeholders = ','.join(['%s'] * len(doc_names))
            query = f"""
                SELECT ORG_NM, UUID_PATH
                FROM {self.okms_doc_table}
                WHERE ORG_NM IN ({placeholders})
            """
            cursor.execute(query, doc_names)
            results = cursor.fetchall()
            cursor.close()

            documents = []
            for row in results:
                documents.append({
                    'id': None,
                    'name': row.get('ORG_NM', ''),
                    'path': row.get('UUID_PATH', '')
                })
            return documents
        except MySQLError as e:
            logger.error(f"Error fetching documents: {e}")
            return []
        finally:
            if conn:
                conn.close()

    def validate_file_path(self, file_path: str) -> Tuple[bool, str]:
        """
        Validate that the file exists and is safe to serve.
        
        Args:
            file_path: File path to validate
            
        Returns:
            Tuple of (is_valid, error_message)
        """
        # Prevent directory traversal
        if '..' in file_path:
            return False, "Invalid file path"

        normalized_path = file_path
        for src_prefix, dst_prefix in self.base_path_aliases.items():
            if normalized_path.startswith(src_prefix + os.sep) or normalized_path == src_prefix:
                normalized_path = normalized_path.replace(src_prefix, dst_prefix, 1)
                break

        real_path = os.path.realpath(normalized_path)
        allowed = False
        for base in self.allowed_base_dirs:
            base_real = os.path.realpath(base)
            if real_path == base_real or real_path.startswith(base_real + os.sep):
                allowed = True
                break

        if not allowed:
            logger.warning(
                f"Blocked file path. real_path={real_path}, allowed_bases={self.allowed_base_dirs}"
            )
            return False, "Invalid file path"
        
        # Check if file exists
        if not os.path.exists(real_path):
            logger.warning(f"File not found: {real_path}")
            return False, "File not found"
        
        # Check if it's a file (not a directory)
        if not os.path.isfile(real_path):
            return False, "Path is not a file"
        
        return True, ""

    def resolve_file_path(self, file_path: str) -> str:
        """Normalize dataset path to the local mounted path."""
        normalized_path = file_path
        for src_prefix, dst_prefix in self.base_path_aliases.items():
            if normalized_path.startswith(src_prefix + os.sep) or normalized_path == src_prefix:
                normalized_path = normalized_path.replace(src_prefix, dst_prefix, 1)
                break
        return os.path.realpath(normalized_path)


_document_service: Optional[DocumentService] = None


def get_document_service(config: Config = None) -> DocumentService:
    """Get or create the global DocumentService instance."""
    global _document_service
    if _document_service is None:
        _document_service = DocumentService(config)
    return _document_service
