"""
Data models for Chat API requests and responses.

Uses Pydantic for validation and serialization.
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field, validator


# ============================================================================
# Request Models
# ============================================================================

class ChatMessage(BaseModel):
    """Chat message model."""

    role: str = Field(..., description="Message role: 'system', 'user', or 'assistant'")
    content: str = Field(..., description="Message content")
    action: Optional[Dict[str, Any]] = Field(None, description="선택 가능한 옵션들")

    class Config:
        json_schema_extra = {
            "example": {
                "role": "user",
                "content": "경상남도의 인구는 몇 명인가요?",
                "action":{
                    "type": "feedback",
                    "options": ["like", "dislike"]
                }
            }
        }


class ChatRequest(BaseModel):
    """Chat completion request model."""

    messages: List[Dict[str, Any]] = Field(
        ...,
        description="List of messages in the conversation"
    )
    user_id: Optional[str] = Field(
        default=None,
        description="User ID for logged-in users (omit for anonymous requests)"
    )
    conv_id: Optional[str] = Field(
        default=None,
        description="Conversation ID for tracking sessions"
    )
    model: Optional[str] = Field(
        default="gsnd-rag-1.0",
        description="Model to use for completion"
    )
    temperature: Optional[float] = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
        description="Temperature for response generation (0.0-2.0)"
    )
    top_p: Optional[float] = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Top-p parameter for nucleus sampling"
    )
    top_k: Optional[int] = Field(
        default=1,
        ge=1,
        description="Top-k parameter for filtering"
    )
    frequency_penalty: Optional[float] = Field(
        default=0.0,
        ge=-2.0,
        le=2.0,
        description="Frequency penalty for token repetition"
    )
    repetition_penalty: Optional[float] = Field(
        default=1.0,
        ge=0.0,
        description="Repetition penalty"
    )
    max_completion_tokens: Optional[int] = Field(
        default=None,
        ge=1,
        description="Maximum tokens for completion"
    )
    seed: Optional[int] = Field(
        default=None,
        description="Random seed for reproducibility"
    )
    stream: Optional[bool] = Field(
        default=False,
        description="Whether to stream the response"
    )
    tools: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Tools available to the model"
    )
    input_type: Optional[str] = Field(
        default="text",
        description="Input type: 'text' or 'voice'"
    )
    mode: Optional[str] = Field(
        default=None,
        description="Optional chat mode (e.g. 'guide_recommend'). /v1/chat/recommended-question 은 mode·의도와 무관하게 전용 경로만 탄다.",
    )

    @validator('messages')
    def validate_messages(cls, v):
        """Validate messages list is not empty."""
        if not v:
            raise ValueError('Messages list cannot be empty')
        return v

    def validate(self) -> tuple[bool, Optional[str]]:
        """
        Additional validation logic.

        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            self.validate_messages(self.messages)
            return True, None
        except ValueError as e:
            return False, str(e)


class RecommendStartRequest(BaseModel):
    """복지서비스추천 버튼 트리거 요청 모델."""

    user_id: Optional[str] = Field(default=None, description="사용자 ID")
    conv_id: Optional[str] = Field(default=None, description="대화 ID (없으면 자동 생성)")
    stream: Optional[bool] = Field(default=False, description="SSE 스트리밍 여부")


# ============================================================================
# Response Models
# ============================================================================

class ErrorDetail(BaseModel):
    """Error detail information."""

    loc: List[Any] = Field(..., description="Error location path")
    msg: str = Field(..., description="Error message")
    type: str = Field(..., description="Error type")


class ErrorResponse(BaseModel):
    """Error response model."""

    detail: List[Dict[str, Any]] = Field(..., description="List of error details")


class ChatChoice(BaseModel):
    """Chat completion choice."""

    index: int = Field(..., description="Choice index")
    message: Dict[str, str] = Field(..., description="Choice message")
    finish_reason: Optional[str] = Field(
        default="stop",
        description="Reason for stopping generation"
    )


class ChatUsage(BaseModel):
    """Token usage information."""

    prompt_tokens: int = Field(..., description="Number of prompt tokens")
    completion_tokens: int = Field(..., description="Number of completion tokens")
    total_tokens: int = Field(..., description="Total number of tokens")


class ChatResponse(BaseModel):
    """Chat completion response model."""

    id: str = Field(..., description="Unique response ID")
    object: str = Field(default="chat.completion", description="Response object type")
    created: int = Field(..., description="Unix timestamp of creation")
    model: str = Field(..., description="Model used for completion")
    choices: List[Dict[str, Any]] = Field(..., description="List of completion choices")
    usage: Dict[str, int] = Field(..., description="Token usage information")
    referenced_documents: Optional[List[Dict[str, str]]] = Field(
        default=None,
        description="List of referenced documents from RAG search with name, id, and snippet"
    )


class DocumentInfo(BaseModel):
    """Document information for download."""

    name: str = Field(..., description="Document name")
    id: str = Field(..., description="Document ID in dataset")
    path: str = Field(..., description="File path on server")


class ExtractAndSummarizeRequest(BaseModel):
    """Request model for extract-and-summarize endpoint."""

    file_path: str = Field(..., description="서버 상의 파일 경로")
    collection_name: Optional[str] = Field(None, description="컬렉션 이름")
    conv_id: str = Field(..., description="Conversation ID")
    user_id: str = Field(..., description="User ID")
