"""HWPX 파싱 & DB 적재 서비스."""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any
import uuid

from app.parsing._db_inserter import insert_rows
from app.parsing._parser_bridge import build_db_rows, parse_hwpx
from app.parsing.schemas import HwpxUploadResponse

logger = logging.getLogger(__name__)


class HwpxParserService:
    """업로드된 HWPX 파일을 파싱하고 DB에 적재한다."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def parse_and_insert(
        self,
        *,
        file_contents: list[tuple[str, bytes]],
        target_table: str,
        welfare_year: str | None,
        region: str | None,
        file_dir: str,
        delete_existing: bool,
        source_uuid_table: str,
    ) -> HwpxUploadResponse:
        all_rows: list[dict] = []
        errors: list[str] = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            for original_name, content in file_contents:
                tmp_path = os.path.join(tmp_dir, original_name)
                try:
                    with open(tmp_path, "wb") as f:
                        f.write(content)
                except OSError as e:
                    errors.append(f"{original_name}: 임시 파일 저장 실패 — {e}")
                    continue

                uuid_nm = uuid.uuid4().hex + ".hwpx"
                try:
                    items = parse_hwpx(
                        tmp_path,
                        welfare_year=welfare_year,
                        region=region,
                        org_nm=original_name,
                        file_dir=file_dir,
                        uuid_nm=uuid_nm,
                    )
                    rows = build_db_rows(items)
                    logger.info("%s: 파싱 %d건 → DB 행 %d건", original_name, len(items), len(rows))
                    all_rows.extend(rows)
                except Exception as e:
                    errors.append(f"{original_name}: 파싱 오류 — {e}")
                    logger.exception("HWPX 파싱 실패: %s", original_name)

        if not all_rows:
            return HwpxUploadResponse(
                target_table=target_table,
                parsed_count=0,
                deleted_count=0,
                inserted_count=0,
                errors=errors,
            )

        try:
            deleted_count, inserted_count = insert_rows(
                self._conn,
                target_table,
                all_rows,
                delete_existing=delete_existing,
                source_uuid_table=source_uuid_table,
            )
            self._conn.commit()
        except Exception as e:
            self._conn.rollback()
            errors.append(f"DB 적재 실패 — {e}")
            logger.exception("DB INSERT 롤백: %s", target_table)
            return HwpxUploadResponse(
                target_table=target_table,
                parsed_count=len(all_rows),
                deleted_count=0,
                inserted_count=0,
                errors=errors,
            )

        return HwpxUploadResponse(
            target_table=target_table,
            parsed_count=len(all_rows),
            deleted_count=deleted_count,
            inserted_count=inserted_count,
            errors=errors,
        )
