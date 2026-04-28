"""
Utility helper functions for the RAG chatbot application.

Provides common utilities for ID generation, token counting, error handling, and response building.
"""

import uuid
import time
import logging
from typing import Dict, Any, List, Optional

import tiktoken

logger = logging.getLogger(__name__)

# Token encoder for accurate token counting
try:
    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception as e:
    logger.warning(f"Failed to load tiktoken encoder: {e}. Using fallback token counting.")
    _ENCODER = None


def generate_id(prefix: str = "chatcmpl") -> str:
    """
    Generate a unique ID for responses.

    Args:
        prefix: Optional prefix for the ID

    Returns:
        Unique ID string
    """
    unique_id = str(uuid.uuid4()).replace('-', '')[:12]
    return f"{prefix}-{unique_id}"


def count_tokens(text: str) -> int:
    """
    Count the number of tokens in text.

    Uses tiktoken if available, otherwise falls back to word-based estimation.

    Args:
        text: Text to count tokens for

    Returns:
        Estimated number of tokens
    """
    if not text:
        return 0

    if _ENCODER:
        try:
            return len(_ENCODER.encode(text))
        except Exception as e:
            logger.warning(f"Failed to count tokens with tiktoken: {e}")

    # Fallback: Rough estimation (approximately 1 token per 4 characters)
    return max(1, len(text) // 4)


def get_current_timestamp() -> int:
    """
    Get current Unix timestamp.

    Returns:
        Current time as Unix timestamp (seconds)
    """
    return int(time.time())


def create_error_detail(
    loc: List[Any],
    msg: str,
    type_: str
) -> Dict[str, Any]:
    """
    Create an error detail dictionary.

    Args:
        loc: Error location path
        msg: Error message
        type_: Error type

    Returns:
        Error detail dictionary
    """
    return {
        "loc": loc,
        "msg": msg,
        "type": type_
    }


def extract_user_message(messages: List[Dict[str, str]]) -> str:
    """
    Extract the last user message from message history.

    Args:
        messages: List of messages

    Returns:
        Content of the last user message, or empty string if not found
    """
    if not messages:
        return ""

    # Find the last user message
    for msg in reversed(messages):
        if msg.get('role') == 'user':
            return msg.get('content', '')

    return ""

def get_last_assistant_message(messages: List[Dict[str, str]]) -> str:
    """
    Extract the last assistant message from message list.

    Args:
        messages: List of messages

    Returns:
        Content of the last assistant message, or empty string if not found
    """
    if not messages:
        return ""

    # Find the last assistant message
    for msg in reversed(messages):
        if msg.get('role') == 'assistant':
            return msg.get('content', '')

    return ""


def get_second_last_user_message(messages: List[Dict[str, str]]) -> str:
    """
    Extract the second-to-last user message (original question before clarification).

    Args:
        messages: List of messages

    Returns:
        Content of the second-to-last user message, or empty string if not found
    """
    if not messages:
        return ""

    user_messages = [msg.get('content', '') for msg in messages if msg.get('role') == 'user']
    
    # If there are at least 2 user messages, return the second-to-last one
    if len(user_messages) >= 2:
        return user_messages[-2]

    return ""


def is_clarification_answer(messages: List[Dict[str, str]]) -> bool:
    """
    Check if current conversation is answering a clarification question.
    
    Returns True if:
    1. There are at least 2 messages in history
    2. The last assistant message ends with '?'
    3. The current user message is likely an answer (not a new question)

    Args:
        messages: List of messages

    Returns:
        True if this appears to be an answer to clarification, False otherwise
    """
    if not messages or len(messages) < 2:
        return False

    last_assistant = get_last_assistant_message(messages[:-1])  # Exclude current user message
    
    # Check if last assistant message was a question (likely clarification)
    if last_assistant and last_assistant.strip().endswith('?'):
        return True

    return False


def count_clarify_attempts(messages: List[Dict[str, str]]) -> int:
    """
    Count the number of clarification (re-ask) attempts in the message history.

    Heuristic: counts assistant messages ending with '?' which are typically
    clarification questions.

    Args:
        messages: List of messages

    Returns:
        Number of clarification attempts detected
    """
    if not messages:
        return 0

    count = 0
    for msg in messages:
        if msg.get('role') != 'assistant':
            continue
        content = (msg.get('content') or '').strip()
        if content.endswith('?'):
            count += 1

    return count

def build_chat_response(
    response_message: str,
    user_message: str,
    model_name: str = "gsnd-rag-1.0",
    referenced_documents: Optional[List[Dict[str, str]]] = None,
    conv_id: Optional[str] = None,
    is_clarification: bool = False,
) -> Dict[str, Any]:
    """
    Build a Chat API response.

    Args:
        response_message: AI's response message
        user_message: User's message
        model_name: Model name used for completion
        referenced_documents: Referenced documents for RAG
        conv_id: Conversation ID

    Returns:
        Chat API response dictionary
    """
    prompt_tokens = count_tokens(user_message)
    completion_tokens = count_tokens(response_message)

    response = {
        "id": generate_id("chatcmpl"),
        "object": "chat.completion",
        "created": get_current_timestamp(),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_message
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens
        }
    }

    if referenced_documents:
        response["referenced_documents"] = referenced_documents

    if conv_id:
        response["conv_id"] = conv_id

    if is_clarification:
        response["is_clarification"] = True

    return response


def shorten_text(text: str, limit: int = 400) -> str:
    """
    Shorten text for logging purposes.

    Args:
        text: Text to shorten
        limit: Maximum length before truncation

    Returns:
        Original text or truncated text with length info
    """
    if not text:
        return ""

    if len(text) <= limit:
        return text

    return text[:limit] + f"...(len={len(text)})"
