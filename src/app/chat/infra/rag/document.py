"""문서 포맷팅 헬퍼"""

import logging
import re
from typing import Any, Dict

from .token import _truncate_text_by_tokens

logger = logging.getLogger(__name__)


def _build_okms_document_name(doc: Dict[str, Any]) -> str:
    """OKMS 문서의 UI 표시용 이름 생성 (ORG_NM 우선, 없으면 BUSINESS_NAME, 없으면 SIGUN/YEAR)"""
    org_nm = str(doc.get("ORG_NM", "") or "").strip()
    if org_nm:
        return org_nm
    business_name = str(doc.get("BUSINESS_NAME", "") or "").strip()
    if business_name:
        return business_name

    sigun = str(doc.get("SIGUN", "") or "").strip()
    year = str(doc.get("YEAR", "") or "").strip()
    parts = []
    if sigun:
        parts.append(sigun)
    if year:
        parts.append(year)
    return " / ".join(parts) or str(doc.get("CHUNK_ID", "") or "문서")


def _get_document_name(doc: Dict[str, Any]) -> str:
    """문서 표시 이름 반환"""
    return str(doc.get("NAME") or _build_okms_document_name(doc)).strip()


def _get_document_snippet(doc: Dict[str, Any]) -> str:
    """문서 본문/스니펫 반환"""
    return str(doc.get("CHUNK_PATH") or doc.get("CONTENT") or "").strip()


def _format_document_for_prompt(doc: Dict[str, Any], index: int, max_doc_chars: int) -> str:
    """최종 응답 프롬프트용 문서 블록 생성"""
    if doc.get("CONTENT") or doc.get("YEAR") or doc.get("SIGUN"):
        sigun = str(doc.get("SIGUN", "") or "").strip() or "정보 없음"
        year = str(doc.get("YEAR", "") or "").strip() or "정보 없음"
        content = _truncate_text_by_tokens(_get_document_snippet(doc), max_doc_chars)
        lines = [
            f"[문서 {index}]",
            f"- 문서명: {_get_document_name(doc)}",
            f"- 지역: {sigun}",
            f"- 작성/시행일: {year}",
        ]
        for field, label in [
            ("DEPARTMENT", "담당부서"),
            ("APPLICATION_PERIOD", "신청기간"),
            ("PURPOSE", "목적"),
            ("TEL", "연락처"),
        ]:
            val = str(doc.get(field, "") or "").strip()
            if val:
                lines.append(f"- {label}: {val}")

        doc_tel = str(doc.get("TEL", "") or "").strip()
        _is_unregistered_tel = (
            doc_tel == "-0000" or doc_tel.endswith("-0000")
            or bool(re.search(r'\d{2,3}-\d{3,4}-0000', content))
        )
        if _is_unregistered_tel:
            content = re.sub(r'\d{2,3}-\d{3,4}-0000\S*', '', content)
        lines.append(f"- 내용: {content}")
        return "\n".join(lines) + "\n\n"

    name = _get_document_name(doc)
    chunk_path = _truncate_text_by_tokens(_get_document_snippet(doc), max_doc_chars)
    return f"[문서 {index}] {name}\n{chunk_path}\n\n"


def _format_facility_for_prompt(doc: Dict[str, Any], index: int) -> str:
    """복지시설 문서를 프롬프트 블록으로 포맷팅"""
    name = str(doc.get("FACILITY_NAME", "") or "").strip() or "정보 없음"
    facility_type = str(doc.get("FACILITY_TYPE", "") or "").strip() or "정보 없음"
    address = str(doc.get("ADDRESS", "") or "").strip() or "정보 없음"
    tel = str(doc.get("TEL", "") or "").strip()
    homepage = str(doc.get("HOMEPAGE", "") or "").strip()

    lines = [
        f"[시설 {index}]",
        f"- 시설명: {name}",
        f"- 시설유형: {facility_type}",
        f"- 주소: {address}",
    ]
    if tel:
        lines.append(f"- 전화: {tel}")
    if homepage:
        lines.append(f"- 홈페이지: {homepage}")
    return "\n".join(lines) + "\n\n"
