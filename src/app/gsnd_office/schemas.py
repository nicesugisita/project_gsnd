"""TB_GSND_OFFICE CRUD 스키마."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GsndOfficeCreate(BaseModel):
    """경남 복지관(사무소) 레코드 생성."""

    sigungu: str | None = Field(None, max_length=50, description="시군구")
    center_nm: str | None = Field(None, max_length=50, description="센터명")
    eupmyeondong: str | None = Field(None, max_length=50, description="읍면동")
    tel_no: str | None = Field(None, max_length=50, description="전화번호")
    address: str | None = Field(None, max_length=50, description="주소")
    id: int | None = Field(None, description="ID (미지정 시 DB AUTO_INCREMENT 사용)")


class GsndOfficeUpdate(BaseModel):
    """경남 복지관 레코드 부분 수정 (PATCH — 모든 필드 선택적)."""

    sigungu: str | None = Field(None, max_length=50)
    center_nm: str | None = Field(None, max_length=50)
    eupmyeondong: str | None = Field(None, max_length=50)
    tel_no: str | None = Field(None, max_length=50)
    address: str | None = Field(None, max_length=50)


class GsndOfficeResponse(BaseModel):
    """경남 복지관 레코드 응답."""

    model_config = ConfigDict(from_attributes=True)

    id: int | None = Field(None, description="레코드 ID")
    sigungu: str | None = Field(None, description="시군구")
    center_nm: str | None = Field(None, description="센터명")
    eupmyeondong: str | None = Field(None, description="읍면동")
    tel_no: str | None = Field(None, description="전화번호")
    address: str | None = Field(None, description="주소")


class GsndOfficeListResponse(BaseModel):
    """경남 복지관 목록 페이지네이션 응답."""

    total: int
    page: int
    page_size: int
    items: list[GsndOfficeResponse]
