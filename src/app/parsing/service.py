"""HWPX 파싱 & DB 적재 서비스."""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any
import uuid

from app.parsing._db_inserter import (
    delete_temp_row_by_sn,
    delete_temp_rows_by_filter,
    insert_rows,
    publish_rows,
    update_temp_row_in_db,
)
from app.parsing._parser_bridge import build_db_rows, parse_hwpx
from app.parsing.schemas import HwpxUploadResponse, PublishResponse
from app.wlf_srvc.repository import WlfSrvcRepository
from app.wlf_srvc.schemas import WlfSrvcListResponse, WlfSrvcResponse

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
        saved_files: list[str] = []

        # HWPX_FILE_DIR 설정 시 해당 경로에 파일 저장
        save_to_disk = bool(file_dir)
        if save_to_disk:
            try:
                os.makedirs(file_dir, exist_ok=True)
            except OSError as e:
                logger.warning("HWPX 저장 디렉터리 생성 실패 (%s): %s — 임시 디렉터리로 폴백", file_dir, e)
                save_to_disk = False

        with tempfile.TemporaryDirectory() as tmp_dir:
            for original_name, content in file_contents:
                uuid_nm = uuid.uuid4().hex + ".hwpx"

                if save_to_disk:
                    dest_path = os.path.join(file_dir, uuid_nm)
                    try:
                        with open(dest_path, "wb") as f:
                            f.write(content)
                        parse_path = dest_path
                        saved_files.append(dest_path)
                    except OSError as e:
                        errors.append(f"{original_name}: 파일 저장 실패 — {e}")
                        logger.warning("파일 저장 실패, 임시 경로로 폴백: %s", original_name)
                        parse_path = os.path.join(tmp_dir, original_name)
                        try:
                            with open(parse_path, "wb") as f:
                                f.write(content)
                        except OSError as e2:
                            errors.append(f"{original_name}: 임시 파일 저장도 실패 — {e2}")
                            continue
                else:
                    parse_path = os.path.join(tmp_dir, original_name)
                    try:
                        with open(parse_path, "wb") as f:
                            f.write(content)
                    except OSError as e:
                        errors.append(f"{original_name}: 임시 파일 저장 실패 — {e}")
                        continue

                try:
                    items = parse_hwpx(
                        parse_path,
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
                saved_files=saved_files,
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
                saved_files=saved_files,
            )

        return HwpxUploadResponse(
            target_table=target_table,
            parsed_count=len(all_rows),
            deleted_count=deleted_count,
            inserted_count=inserted_count,
            errors=errors,
            saved_files=saved_files,
        )

    def list_temp_rows(
        self,
        *,
        temp_table: str,
        wlf_yr: str | None = None,
        sigun_cd: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> WlfSrvcListResponse:
        repo = WlfSrvcRepository(self._conn, table=temp_table)
        rows, total = repo.list(
            wlf_yr=wlf_yr,
            sigun_cd=sigun_cd,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
        items = [WlfSrvcResponse(**row) for row in rows]
        return WlfSrvcListResponse(total=total, page=page, page_size=page_size, items=items)

    def update_temp_row(
        self,
        *,
        temp_table: str,
        sn: int,
        data: dict,
    ) -> WlfSrvcResponse | None:
        updated = update_temp_row_in_db(self._conn, table=temp_table, sn=sn, data=data)
        if not updated:
            return None
        self._conn.commit()
        repo = WlfSrvcRepository(self._conn, table=temp_table)
        row = repo.get(sn)
        return WlfSrvcResponse(**row) if row else None

    def delete_row(self, *, temp_table: str, sn: int) -> bool:
        deleted = delete_temp_row_by_sn(self._conn, table=temp_table, sn=sn)
        if deleted:
            self._conn.commit()
        return deleted

    def delete_rows(self, *, temp_table: str, wlf_yr: str | None) -> int:
        count = delete_temp_rows_by_filter(self._conn, table=temp_table, wlf_yr=wlf_yr)
        self._conn.commit()
        return count

    def publish_to_real_table(
        self,
        *,
        source_table: str,
        target_table: str,
        year: str | None,
        org_names: list[str] | None,
    ) -> PublishResponse:
        try:
            deleted_count, inserted_count, published_org_names = publish_rows(
                self._conn,
                source_table=source_table,
                target_table=target_table,
                year=year,
                org_names=org_names,
            )
            self._conn.commit()
        except Exception as e:
            self._conn.rollback()
            logger.exception("publish 롤백: %s → %s", source_table, target_table)
            raise RuntimeError(f"반영 실패 — {e}") from e

        return PublishResponse(
            source_table=source_table,
            target_table=target_table,
            deleted_count=deleted_count,
            inserted_count=inserted_count,
            published_org_names=published_org_names,
        )
