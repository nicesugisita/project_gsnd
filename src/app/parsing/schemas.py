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
    saved_files: list[str] = Field(default_factory=list, description="영구 저장된 파일 경로 목록 (UPLOAD_DIR 미설정 시 빈 배열)")


class PublishRequest(BaseModel):
    """temp → 실 테이블 반영 요청."""

    year: str | None = Field(None, description="반영할 복지연도 (예: '2026'). None이면 전체 연도.")
    org_names: list[str] | None = Field(None, description="반영할 ORG_NM 목록. None이면 전체.")


class PublishResponse(BaseModel):
    """temp → 실 테이블 반영 결과."""

    source_table: str = Field(description="원본 테이블 (temp)")
    target_table: str = Field(description="반영 대상 테이블 (실 테이블)")
    deleted_count: int = Field(description="실 테이블에서 삭제된 기존 행 수")
    inserted_count: int = Field(description="실 테이블에 새로 INSERT된 행 수")
    published_org_names: list[str] = Field(description="반영된 ORG_NM 목록")


class DeleteTempRowsResponse(BaseModel):
    """temp 테이블 행 삭제 결과."""

    table: str = Field(description="삭제 대상 테이블명")
    deleted_count: int = Field(description="삭제된 행 수")


class TempRowUpdate(BaseModel):
    """temp 테이블 행 수정 — 콘텐츠 필드 전체."""

    wlf_yr: str | None = None
    wlf_srvc_nm: str | None = None
    sigun_cd: str | None = None
    org_nm: str | None = None
    lftm_cycl_cd: str | None = None
    hshd_sttn_cd: str | None = None
    wlf_srvc_cn: str | None = None
    aply_yn: str | None = None
    aply_bgng_dt: str | None = None
    aply_end_dt: str | None = None
    aply_prd_type: str | None = None
    use_yn: str | None = None
    aply_qlfc: str | None = None
    bss: str | None = None
    prps: str | None = None
    pvsn_type: str | None = None
    sprt_cn: str | None = None
    sprt_trgt: str | None = None
    enfc_mnbd: str | None = None
    aply_mthd: str | None = None
    inqpl: str | None = None
    tkcg_dept: str | None = None
    telno: str | None = None
    sbmsn_dcmnt: str | None = None
    sprt_trgt_cn: str | None = None
    enfc_mnbd_cn: str | None = None
    remark: str | None = None
    itrst_tpc1: str | None = None
    itrst_tpc2: str | None = None
    file_dir: str | None = None
    uuid_nm: str | None = None
