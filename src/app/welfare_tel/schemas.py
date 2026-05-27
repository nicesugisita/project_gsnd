"""WELFARE_TEL CRUD 스키마."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WelfareTelCreate(BaseModel):
    """연락처 레코드 생성."""

    old_id: int | None = Field(None, description="기존 순번")
    region: str | None = Field(None, max_length=50, description="지역")
    project_name: str | None = Field(None, max_length=50, description="사업명")
    doc_name: str | None = Field(None, max_length=50, description="문서명")
    contact_dept: str | None = Field(None, max_length=50, description="문의처 및 담당부서")
    phone: str | None = Field(None, max_length=50, description="연락처")
    duties: str | None = Field(None, max_length=50, description="담당업무")
    year: str | None = Field(None, max_length=50, description="연도")


class WelfareTelUpdate(BaseModel):
    """연락처 레코드 부분 수정 (PATCH — 모든 필드 선택적)."""

    old_id: int | None = None
    region: str | None = Field(None, max_length=50)
    project_name: str | None = Field(None, max_length=50)
    doc_name: str | None = Field(None, max_length=50)
    contact_dept: str | None = Field(None, max_length=50)
    phone: str | None = Field(None, max_length=50)
    duties: str | None = Field(None, max_length=50)
    year: str | None = Field(None, max_length=50)


class WelfareTelResponse(BaseModel):
    """연락처 레코드 응답."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="순번 (순번_NEW)")
    old_id: int | None = Field(None, description="기존 순번")
    region: str | None = Field(None, description="지역")
    project_name: str | None = Field(None, description="사업명")
    doc_name: str | None = Field(None, description="문서명")
    contact_dept: str | None = Field(None, description="문의처 및 담당부서")
    phone: str | None = Field(None, description="연락처")
    duties: str | None = Field(None, description="담당업무")
    year: str | None = Field(None, description="연도")
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WelfareTelListResponse(BaseModel):
    """연락처 목록 페이지네이션 응답."""

    total: int
    page: int
    page_size: int
    items: list[WelfareTelResponse]
