"""
Streaming wrapper for adding status messages to the chat completion process.
"""

import logging
from typing import AsyncGenerator, Any, Dict

from utils.status_messages import (
    build_status_message,
    STATUS_TEXT_CLEANING,
    STATUS_KOREAN_CONVERT,
    STATUS_ASK_JUDGMENT,
    STATUS_RE_ASK,
    STATUS_QUERY_RECREATION,
    STATUS_RAG_NORAG_JUDGMENT,
)

logger = logging.getLogger(__name__)


class StreamingStatusWrapper:
    """
    Wrapper that adds status messages to the streaming chat completion process.
    """
    
    def __init__(self):
        """Initialize the wrapper."""
        self.status_enabled = False
    
    def enable(self):
        """Enable status message emission."""
        self.status_enabled = True
    
    async def emit(self, message: str):
        """
        Emit a status message if enabled.
        
        Args:
            message: Status message to emit
            
        Yields:
            Status message in SSE format
        """
        if self.status_enabled:
            yield build_status_message(message)
    
    async def wrap_generator(self, generator: AsyncGenerator, final_status: str = None) -> AsyncGenerator:
        """
        Wrap an async generator with optional final status message.
        
        Args:
            generator: Original async generator
            final_status: Optional status message to emit before generator
            
        Yields:
            Status message (if provided) then generator items
        """
        if final_status and self.status_enabled:
            yield build_status_message(final_status)
        
        async for item in generator:
            yield item


# Global instance
_status_wrapper = StreamingStatusWrapper()


def get_status_wrapper() -> StreamingStatusWrapper:
    """
    Get the global status wrapper instance.
    
    Returns:
        Global StreamingStatusWrapper instance
    """
    return _status_wrapper
