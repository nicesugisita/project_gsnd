"""
Status message constants and utilities for streaming progress updates.
"""

import json
from typing import AsyncGenerator


# Status message constants
STATUS_TEXT_CLEANING = "질문을 분석하고 있습니다"
STATUS_KOREAN_CONVERT = "질문을 분석하고 있습니다"
STATUS_ASK_JUDGMENT = "질문을 분석하고 있습니다"
STATUS_RE_ASK = "질문을 분석하고 있습니다"
STATUS_QUERY_RECREATION = "질문을 분석하고 있습니다"
STATUS_RAG_NORAG_JUDGMENT = "질문을 분석하고 있습니다"
STATUS_INTENT_CLASSIFICATION = "질문을 분석하고 있습니다"
STATUS_QUERY_REFORM = "최적의 답변방식을 찾고 있습니다"
STATUS_QUERY_EXPANSION = "최적의 답변방식을 찾고 있습니다"
STATUS_TRIPLE_EXTRACTION = "내용을 정리하고 있습니다"
STATUS_FINAL_RESPONSE = "최종답변을 생성하고 있습니다"


def build_status_message(message: str) -> str:
    """
    Build a status update message in SSE format.

    Args:
        message: Status message to send

    Returns:
        Formatted SSE status string
    """
    return f"data: {json.dumps({'type': 'status', 'message': message}, ensure_ascii=False)}\n\n"


class StatusEmitter:
    """
    Helper class for emitting status messages during streaming.
    """
    
    def __init__(self, stream_enabled: bool = False):
        """
        Initialize status emitter.
        
        Args:
            stream_enabled: Whether streaming is enabled
        """
        self.stream_enabled = stream_enabled
        self.status_queue = []
    
    async def emit(self, message: str):
        """
        Emit a status message if streaming is enabled.
        
        Args:
            message: Status message to emit
        """
        if self.stream_enabled:
            self.status_queue.append(build_status_message(message))
    
    async def get_pending_messages(self) -> AsyncGenerator[str, None]:
        """
        Get all pending status messages and clear the queue.
        
        Yields:
            Pending status messages
        """
        while self.status_queue:
            yield self.status_queue.pop(0)
