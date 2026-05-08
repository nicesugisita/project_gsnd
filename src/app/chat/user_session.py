"""
User session management service for concurrent user limit.

Handles tracking active users and enforcing the 50-user concurrent limit.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

import mysql.connector
from mysql.connector import Error as MySQLError

from app.core.config import Config

logger = logging.getLogger(__name__)

MAX_CONCURRENT_USERS = Config.MAX_CONCURRENT_USERS
SESSION_TIMEOUT_MINUTES = Config.SESSION_TIMEOUT_MINUTES
SESSION_TABLE = Config.SESSION_TABLE


class UserSessionService:
    """Service for managing user sessions and enforcing concurrent user limits."""

    def __init__(self, config: Config = None):
        """
        Initialize the UserSessionService.

        Args:
            config: Application configuration object
        """
        if config is None:
            config = Config
        
        self.config = config
        self.db_host = config.DB_HOST
        self.db_user = config.DB_USER
        self.db_password = config.DB_PASSWORD
        self.db_name = config.DB_NAME
        self.db_port = config.DB_PORT

    def _get_connection(self):
        """
        Get a MySQL connection.

        Returns:
            MySQL connection object

        Raises:
            MySQLError: If connection fails
        """
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

    def _cleanup_expired_sessions(self, conn=None):
        """
        Remove expired sessions from the database.

        Args:
            conn: Optional MySQL connection. If None, creates a new one.
        """
        close_conn = False
        try:
            if conn is None:
                conn = self._get_connection()
                close_conn = True

            cursor = conn.cursor()
            timeout_threshold = datetime.now() - timedelta(minutes=SESSION_TIMEOUT_MINUTES)
            
            query = f"""
                DELETE FROM {SESSION_TABLE} 
                WHERE last_activity < %s
            """
            cursor.execute(query, (timeout_threshold,))
            deleted_count = cursor.rowcount
            
            if deleted_count > 0:
                logger.debug(f"Cleaned up {deleted_count} expired sessions")
            
            cursor.close()
        except MySQLError as e:
            logger.error(f"Error cleaning up expired sessions: {e}")
        finally:
            if close_conn and conn:
                conn.close()

    def check_user_limit(self, user_identifier: str) -> Tuple[bool, Optional[str]]:
        """
        Check if a user can access the service based on concurrent user limit.

        Args:
            user_identifier: User ID or conv_id

        Returns:
            Tuple of (is_allowed, error_message)
            - is_allowed: True if user can access, False if limit exceeded
            - error_message: Error message if not allowed, None otherwise
        """
        conn = None
        try:
            conn = self._get_connection()
            
            # Clean up expired sessions first
            self._cleanup_expired_sessions(conn)
            
            cursor = conn.cursor()
            
            # Check if user session already exists and is active
            check_query = f"""
                SELECT id FROM {SESSION_TABLE}
                WHERE user_identifier = %s
                AND last_activity > %s
            """
            expiry_threshold = datetime.now() - timedelta(minutes=SESSION_TIMEOUT_MINUTES)
            cursor.execute(check_query, (user_identifier, expiry_threshold))
            existing_session = cursor.fetchone()
            
            if existing_session:
                # User already has an active session, update last_activity
                update_query = f"""
                    UPDATE {SESSION_TABLE} 
                    SET last_activity = %s 
                    WHERE user_identifier = %s
                """
                cursor.execute(update_query, (datetime.now(), user_identifier))
                cursor.close()
                return True, None
            
            # Count current active users (excluding the requesting user)
            count_query = f"""
                SELECT COUNT(DISTINCT user_identifier) FROM {SESSION_TABLE}
                WHERE last_activity > %s
            """
            cursor.execute(count_query, (expiry_threshold,))
            active_user_count = cursor.fetchone()[0]
            
            if active_user_count >= MAX_CONCURRENT_USERS:
                cursor.close()
                error_msg = "현재 사용자 수가 많아 서버가 부하 상태입니다. 조금 후에 사용해 주세요."
                logger.warning(
                    f"Concurrent user limit reached. "
                    f"Active users: {active_user_count}, "
                    f"Requested by: {user_identifier}"
                )
                return False, error_msg
            
            # Create new session for the user
            insert_query = f"""
                INSERT INTO {SESSION_TABLE} (user_identifier, created_at, last_activity)
                VALUES (%s, %s, %s)
            """
            now = datetime.now()
            cursor.execute(insert_query, (user_identifier, now, now))
            cursor.close()
            
            logger.info(
                f"New user session created. "
                f"User: {user_identifier}, "
                f"Active users: {active_user_count + 1}"
            )
            return True, None

        except MySQLError as e:
            logger.error(f"Database error in check_user_limit: {e}")
            # On database error, allow the request to proceed (fail open)
            return True, None
        finally:
            if conn:
                conn.close()

    def update_user_activity(self, user_identifier: str) -> bool:
        """
        Update the last activity timestamp for a user.

        Args:
            user_identifier: User ID or conv_id

        Returns:
            True if update successful, False otherwise
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            update_query = f"""
                UPDATE {SESSION_TABLE} 
                SET last_activity = %s 
                WHERE user_identifier = %s
            """
            cursor.execute(update_query, (datetime.now(), user_identifier))
            cursor.close()
            
            return True
        except MySQLError as e:
            logger.error(f"Error updating user activity: {e}")
            return False
        finally:
            if conn:
                conn.close()

    def get_active_user_count(self) -> int:
        """
        Get the current count of active users.

        Returns:
            Number of active users
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # Clean up expired sessions
            self._cleanup_expired_sessions(conn)
            
            query = f"""
                SELECT COUNT(DISTINCT user_identifier) FROM {SESSION_TABLE}
                WHERE last_activity > %s
            """
            expiry_threshold = datetime.now() - timedelta(minutes=SESSION_TIMEOUT_MINUTES)
            cursor.execute(query, (expiry_threshold,))
            count = cursor.fetchone()[0]
            cursor.close()
            
            return count
        except MySQLError as e:
            logger.error(f"Error getting active user count: {e}")
            return 0
        finally:
            if conn:
                conn.close()

    def cleanup_user_session(self, user_identifier: str) -> bool:
        """
        Remove a user session when they disconnect.

        Args:
            user_identifier: User ID or conv_id

        Returns:
            True if deletion successful, False otherwise
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            delete_query = f"""
                DELETE FROM {SESSION_TABLE} 
                WHERE user_identifier = %s
            """
            cursor.execute(delete_query, (user_identifier,))
            cursor.close()
            
            logger.info(f"User session cleaned up: {user_identifier}")
            return True
        except MySQLError as e:
            logger.error(f"Error cleaning up user session: {e}")
            return False
        finally:
            if conn:
                conn.close()

    @staticmethod
    def create_tables(connection=None) -> bool:
        """
        Create the user_sessions table if it doesn't exist.

        Args:
            connection: Optional MySQL connection

        Returns:
            True if table created/exists, False on error
        """
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
                CREATE TABLE IF NOT EXISTS {SESSION_TABLE} (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_identifier VARCHAR(255) NOT NULL UNIQUE,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_activity DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_created_at (created_at),
                    INDEX idx_last_activity (last_activity)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
            cursor.execute(create_table_query)
            cursor.close()
            
            logger.info(f"{SESSION_TABLE} table created or already exists")
            return True
        except MySQLError as e:
            logger.error(f"Error creating user_sessions table: {e}")
            return False
        finally:
            if close_conn and conn:
                conn.close()


# Global service instance
_user_session_service: Optional[UserSessionService] = None


def get_user_session_service(config: Config = None) -> UserSessionService:
    """
    Get or create the global UserSessionService instance.

    Args:
        config: Application configuration object

    Returns:
        UserSessionService instance
    """
    global _user_session_service
    if _user_session_service is None:
        _user_session_service = UserSessionService(config)
        # Create tables on initialization
        UserSessionService.create_tables()
    return _user_session_service
