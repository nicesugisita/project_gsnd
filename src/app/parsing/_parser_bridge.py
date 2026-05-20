"""_hwpx_parser_lib의 파싱 함수를 래핑하는 브리지 레이어."""

from __future__ import annotations

from app.parsing._hwpx_parser_lib import _parse_hwpx, build_db_rows


def parse_hwpx(
    hwpx_path: str,
    welfare_year: str | None,
    region: str | None,
    org_nm: str,
    file_dir: str,
    uuid_nm: str,
) -> list[dict]:
    """단일 HWPX 파일을 파싱해 한글 필드명 item list를 반환한다."""
    return _parse_hwpx(
        hwpx_path,
        welfare_year=welfare_year,
        region=region,
        org_nm=org_nm,
        file_dir=file_dir,
        uuid_nm=uuid_nm,
    )


__all__ = ["build_db_rows", "parse_hwpx"]
