"""
ChatHistoryRepository — 주입받은 DB 커넥션으로 히스토리를 CRUD.

get_repository() 의존성에서 생성되며, 요청 단위로 커넥션이 바인딩된다.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_HISTORY_TABLE = "gsnd_chat_history"
_ANON_TABLE    = "gsnd_chat_history_anonymous"
_ANON_MARKER   = "__anonymous__"


class ChatHistoryRepository:
    """
    DB 커넥션을 생성자로 주입받는 레포지터리.

    Mock 교체 시 ChatHistoryRepositoryProtocol을 구현한 객체를 주입.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def _table(self, user_id: str) -> str:
        return _ANON_TABLE if user_id == _ANON_MARKER else _HISTORY_TABLE

    def upsert_history(
        self,
        user_id: str,
        conv_id: str,
        messages: List[Dict[str, Any]],
        overwrite: bool = False,
    ) -> None:
        """히스토리 저장 (없으면 INSERT, 있으면 UPDATE)."""
        table = self._table(user_id)
        messages_json = json.dumps(messages, ensure_ascii=False)
        try:
            cursor = self._conn.cursor()
            if overwrite:
                cursor.execute(
                    f"""INSERT INTO {table} (user_id, conv_id, messages, updated_at)
                        VALUES (%s, %s, %s, NOW())
                        ON DUPLICATE KEY UPDATE messages = VALUES(messages), updated_at = NOW()""",
                    (user_id, conv_id, messages_json),
                )
            else:
                cursor.execute(
                    f"SELECT messages FROM {table} WHERE user_id=%s AND conv_id=%s",
                    (user_id, conv_id),
                )
                row = cursor.fetchone()
                if row:
                    existing = json.loads(row[0] if isinstance(row, tuple) else row["messages"])
                    merged = {m["role"] + m.get("content", ""): m for m in existing + messages}
                    messages_json = json.dumps(list(merged.values()), ensure_ascii=False)
                cursor.execute(
                    f"""INSERT INTO {table} (user_id, conv_id, messages, updated_at)
                        VALUES (%s, %s, %s, NOW())
                        ON DUPLICATE KEY UPDATE messages = VALUES(messages), updated_at = NOW()""",
                    (user_id, conv_id, messages_json),
                )
            cursor.close()
        except Exception:
            logger.exception("[Repository] upsert_history 실패: conv_id=%s", conv_id)
            raise

    def get_history(self, user_id: str, conv_id: str) -> List[Dict[str, Any]]:
        """저장된 히스토리 반환. 없으면 빈 리스트."""
        table = self._table(user_id)
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                f"SELECT messages FROM {table} WHERE user_id=%s AND conv_id=%s",
                (user_id, conv_id),
            )
            row = cursor.fetchone()
            cursor.close()
            if not row:
                return []
            raw = row[0] if isinstance(row, tuple) else row["messages"]
            return json.loads(raw) if raw else []
        except Exception:
            logger.exception("[Repository] get_history 실패: conv_id=%s", conv_id)
            return []

    def delete_history(self, user_id: str, conv_id: str) -> None:
        """히스토리 삭제."""
        table = self._table(user_id)
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                f"DELETE FROM {table} WHERE user_id=%s AND conv_id=%s",
                (user_id, conv_id),
            )
            cursor.close()
        except Exception:
            logger.exception("[Repository] delete_history 실패: conv_id=%s", conv_id)
            raise
