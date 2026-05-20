"""복지서비스 CRUD 스키마."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WlfSrvcUpdate(BaseModel):
    """관리자가 수정 가능한 필드만 포함."""

    wlf_yr: str | None = Field(None, max_length=4, description="복지연도")
    wlf_srvc_nm: str | None = Field(None, max_length=100, description="복지서비스명")
    sigun_cd: str | None = Field(None, max_length=10, description="시군코드")
    use_yn: str | None = Field(None, max_length=1, description="사용여부 (Y/N)")
    aply_yn: str | None = Field(None, max_length=1, description="신청여부 (Y/N)")
    remark: str | None = Field(None, max_length=100, description="비고")
    itrst_tpc1: str | None = Field(None, max_length=100, description="관심분야1")
    itrst_tpc2: str | None = Field(None, max_length=100, description="관심분야2")
    telno: str | None = Field(None, max_length=100, description="전화번호")
    tkcg_dept: str | None = Field(None, max_length=1000, description="담당부서")
    inqpl: str | None = Field(None, max_length=2000, description="문의처")


class WlfSrvcResponse(BaseModel):
    """복지서비스 단건 응답 — DDL 전체 컬럼."""

    model_config = ConfigDict(from_attributes=True)

    # ── PK ─────────────────────────────────────────────────────────────────
    id: int = Field(description="WLF_SRVC_SN")

    # ── 기본 정보 ───────────────────────────────────────────────────────────
    wlf_srvc_aply_form_sn: int | None = None
    wlf_yr: str | None = None
    wlf_srvc_nm: str | None = None
    lftm_cycl_cd: str | None = None
    hshd_sttn_cd: str | None = None
    sigun_cd: str | None = None

    # ── 이미지 ─────────────────────────────────────────────────────────────
    img_orgnl_file_nm: str | None = None
    img_file_nm: str | None = None
    img_file_path_nm: str | None = None

    # ── 서비스 내용 ─────────────────────────────────────────────────────────
    wlf_srvc_cn: str | None = None
    aply_yn: str | None = None
    aply_bgng_dt: str | None = None
    aply_end_dt: str | None = None
    aply_prd_type: str | None = None

    # ── 등록자 정보 ─────────────────────────────────────────────────────────
    rgtr_sn: int | None = None
    rgtr_id: str | None = None
    rgtr_nm: str | None = None
    rgtr_ip_addr: str | None = None
    rgtr_brwsr: str | None = None
    reg_dt: datetime | None = None

    # ── 수정자 정보 ─────────────────────────────────────────────────────────
    mdfr_sn: int | None = None
    mdfr_id: str | None = None
    mdfr_nm: str | None = None
    mdfr_ip_addr: str | None = None
    mdfr_brwsr: str | None = None
    mdfcn_dt: datetime | None = None

    # ── 사용 / 통계 ─────────────────────────────────────────────────────────
    use_yn: str | None = None
    inq_cnt: int | None = None

    # ── 세부 정보 ───────────────────────────────────────────────────────────
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
    itrst_tpc: str | None = None

    # ── 파일 / 관심분야 / 기타 ──────────────────────────────────────────────
    org_nm: str | None = None
    file_dir: str | None = None
    uuid_nm: str | None = None
    remark: str | None = None
    itrst_tpc1: str | None = None
    itrst_tpc2: str | None = None


class WlfSrvcListResponse(BaseModel):
    """복지서비스 목록 페이지네이션 응답."""

    total: int
    page: int
    page_size: int
    items: list[WlfSrvcResponse]
