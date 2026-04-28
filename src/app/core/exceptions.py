"""
Custom exception classes for the RAG chatbot application.
"""


class RAGApplicationError(Exception):
    """Base exception class for RAG application errors."""
    pass


class LLMServiceError(RAGApplicationError):
    """Raised when LLM service encounters an error."""
    pass


class RAGServiceError(RAGApplicationError):
    """Raised when RAG service encounters an error."""
    pass


class ConfigurationError(RAGApplicationError):
    """Raised when configuration is invalid or missing."""
    pass


class PromptLoadError(RAGApplicationError):
    """Raised when a prompt file cannot be loaded."""
    pass


class ValidationError(RAGApplicationError):
    """Raised when request validation fails."""
    pass


class StreamingError(RAGApplicationError):
    """Raised when streaming response encounters an error."""
    pass
