"""TB_OFFICE CRUD 스키마."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OfficeCreate(BaseModel):
    """시설(사무소) 레코드 생성."""

    sigun: str | None = Field(None, max_length=50, description="시군")
    facility_name: str | None = Field(None, max_length=50, description="시설명")
    coord_x: str | None = Field(None, max_length=50, description="경위도 X좌표")
    coord_y: str | None = Field(None, max_length=50, description="경위도 Y좌표")
    refined_road_address: str | None = Field(None, max_length=200, description="정제도로명주소")
    refined_lot_address: str | None = Field(None, max_length=200, description="정제지번주소")
    location: str | None = Field(None, max_length=200, description="소재지")
    facility_type: str | None = Field(None, max_length=50, description="시설종류")
    facility_category: str | None = Field(None, max_length=50, description="시설분류")
    capacity: str | None = Field(None, max_length=50, description="정원")
    category_large: str | None = Field(None, max_length=50, description="대분류")
    category_small: str | None = Field(None, max_length=50, description="소분류")
    category_large2: str | None = Field(None, max_length=50, description="대분류2")
    category_small2: str | None = Field(None, max_length=50, description="소분류2")
    category_large3: str | None = Field(None, max_length=50, description="대분류3")
    category_small3: str | None = Field(None, max_length=50, description="소분류3")
    homepage: str | None = Field(None, max_length=200, description="홈페이지")
    tel: str | None = Field(None, max_length=200, description="전화번호")


class OfficeUpdate(BaseModel):
    """시설 레코드 부분 수정 (PATCH — 모든 필드 선택적)."""

    sigun: str | None = Field(None, max_length=50)
    facility_name: str | None = Field(None, max_length=50)
    coord_x: str | None = Field(None, max_length=50)
    coord_y: str | None = Field(None, max_length=50)
    refined_road_address: str | None = Field(None, max_length=200)
    refined_lot_address: str | None = Field(None, max_length=200)
    location: str | None = Field(None, max_length=200)
    facility_type: str | None = Field(None, max_length=50)
    facility_category: str | None = Field(None, max_length=50)
    capacity: str | None = Field(None, max_length=50)
    category_large: str | None = Field(None, max_length=50)
    category_small: str | None = Field(None, max_length=50)
    category_large2: str | None = Field(None, max_length=50)
    category_small2: str | None = Field(None, max_length=50)
    category_large3: str | None = Field(None, max_length=50)
    category_small3: str | None = Field(None, max_length=50)
    homepage: str | None = Field(None, max_length=200)
    tel: str | None = Field(None, max_length=200)


class OfficeResponse(BaseModel):
    """시설 레코드 응답."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="레코드 ID")
    sigun: str | None = Field(None, description="시군")
    facility_name: str | None = Field(None, description="시설명")
    coord_x: str | None = Field(None, description="경위도 X좌표")
    coord_y: str | None = Field(None, description="경위도 Y좌표")
    refined_road_address: str | None = Field(None, description="정제도로명주소")
    refined_lot_address: str | None = Field(None, description="정제지번주소")
    location: str | None = Field(None, description="소재지")
    facility_type: str | None = Field(None, description="시설종류")
    facility_category: str | None = Field(None, description="시설분류")
    capacity: str | None = Field(None, description="정원")
    category_large: str | None = Field(None, description="대분류")
    category_small: str | None = Field(None, description="소분류")
    category_large2: str | None = Field(None, description="대분류2")
    category_small2: str | None = Field(None, description="소분류2")
    category_large3: str | None = Field(None, description="대분류3")
    category_small3: str | None = Field(None, description="소분류3")
    homepage: str | None = Field(None, description="홈페이지")
    tel: str | None = Field(None, description="전화번호")
    created_at: datetime | None = None
    updated_at: datetime | None = None


class OfficeListResponse(BaseModel):
    """시설 목록 페이지네이션 응답."""

    total: int
    page: int
    page_size: int
    items: list[OfficeResponse]
