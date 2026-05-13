"""GSND_OUR_REGION_TEL 컬렉션의 시군별 등록 데이터 점검 스크립트.

특정 시군에 어떤 읍면동·기관·전화번호가 등록되어 있는지 dump 한다.
누락된 읍면동을 찾을 때 (예: "상북면 행정복지센터 연락처 알려줘" 가 0건 반환되는 사례) 사용.

사용:
    cd backend-qa
    python tests/check_welfare_tel_data.py "경상남도 함안군"
    python tests/check_welfare_tel_data.py "경상남도 창원시" "경상남도 김해시"

필요 환경:
    - .env (Mariner 서버 접속 정보)
    - JVM/JPype (queryset_welfare_tel 가 사용)
"""
from __future__ import annotations

import sys
from pathlib import Path


def _ensure_app_path_on_sys() -> None:
    """src/ 를 sys.path 에 추가 (프로젝트 루트에서 실행할 때 import 가능하도록)."""
    root = Path(__file__).resolve().parents[1]
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


_ensure_app_path_on_sys()

from app.mariner.queryset_welfare_tel import query_welfare_tel_documents  # noqa: E402


def dump_sigun(sigun: str) -> None:
    print(f"\n========== {sigun} ==========")
    docs = query_welfare_tel_documents(
        keyword=sigun,                    # 검색어는 사용 안 됨(필터 위주). sigun 넣어 둠.
        sigun_filters=[sigun],
        eupmyeondong_filters=None,        # 전체
        max_results=-1,                    # unbounded
    )
    if not docs:
        print("  (등록 데이터 없음)")
        return

    # EUPMYEONDONG 기준 정렬
    docs_sorted = sorted(docs, key=lambda d: str(d.get("EUPMYEONDONG", "") or ""))
    print(f"  총 {len(docs_sorted)}건\n")
    print(f"  {'EUPMYEONDONG':<14} | {'CENTER':<30} | {'TEL':<14} | ADDRESS")
    print(f"  {'-'*14} | {'-'*30} | {'-'*14} | {'-'*40}")
    for d in docs_sorted:
        eup = str(d.get("EUPMYEONDONG", "") or "")
        center = str(d.get("CENTER", "") or "")
        tel = str(d.get("TEL", "") or "")
        addr = str(d.get("ADDRESS", "") or "")
        print(f"  {eup:<14} | {center:<30} | {tel:<14} | {addr}")

    # 중복/유일 읍면동 카운트
    unique_eup = sorted({str(d.get("EUPMYEONDONG", "") or "") for d in docs_sorted})
    print(f"\n  unique EUPMYEONDONG ({len(unique_eup)}): {unique_eup}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python tests/check_welfare_tel_data.py <SIGUN> [SIGUN2] ...")
        print('  예: python tests/check_welfare_tel_data.py "경상남도 함안군"')
        return 1
    for sigun in argv[1:]:
        dump_sigun(sigun)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
