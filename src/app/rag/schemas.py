"""RAG 쿼리 유틸 스키마."""
from pydantic import BaseModel


class ExtractTriplesRequest(BaseModel):
    question: str


class ExpandQueryRequest(BaseModel):
    question: str


class ReQueryRequest(BaseModel):
    question: str
    user_conv_id: str = ""


class ComparisonAttributeRequest(BaseModel):
    question: str


class ComparisonTripleRequest(BaseModel):
    question: str
