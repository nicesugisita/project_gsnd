"""Logging context helpers for request-scoped fields."""

import logging
from contextvars import ContextVar, Token
from typing import Optional

_conv_id_var: ContextVar[str] = ContextVar("conv_id", default="-")
_user_id_var: ContextVar[str] = ContextVar("user_id", default="-")


def set_log_context(conv_id: Optional[str] = None, user_id: Optional[str] = None) -> tuple[Token, Token]:
    """Set per-request logging context values and return reset tokens."""
    conv_value = str(conv_id).strip() if conv_id else "-"
    user_value = str(user_id).strip() if user_id else "-"
    conv_token = _conv_id_var.set(conv_value)
    user_token = _user_id_var.set(user_value)
    return conv_token, user_token


def reset_log_context(tokens: tuple[Token, Token]) -> None:
    """Reset logging context using previously returned tokens."""
    conv_token, user_token = tokens
    _conv_id_var.reset(conv_token)
    _user_id_var.reset(user_token)


class RequestContextFilter(logging.Filter):
    """Inject request context fields into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        record.conv_id = _conv_id_var.get("-")
        record.user_id = _user_id_var.get("-")
        return True
