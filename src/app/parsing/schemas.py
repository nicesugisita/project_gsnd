"""HWPX 파싱 API 스키마."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HwpxUploadResponse(BaseModel):
    """HWPX 파일 업로드 및 DB 적재 결과."""

    target_table: str | None = Field(None, description="적재 대상 테이블명 (dry_run=True이면 null)")
    parsed_count: int = Field(description="파싱된 복지서비스 레코드 수")
    deleted_count: int = Field(0, description="기존 ORG_NM 행 삭제 수 (delete_existing=True일 때만)")
    inserted_count: int = Field(description="실제 INSERT된 레코드 수")
    errors: list[str] = Field(default_factory=list, description="파일별 파싱/적재 오류 메시지")
