"""
parse_hwpx_v6.py
────────────────
HWPX 원문을 읽어 복지서비스 항목을 추출하고, DB 컬럼 형태의 row dict로 변환한다.

이 파일의 역할:
  · 단독 실행 시: 2.시군/3.유관기관 폴더를 파싱해 JSON + INSERT SQL 파일을 생성한다.
  · parse_hwpx_v6_db.py에서 import 시: 엑셀 로드, HWPX 파싱, DB row 변환 함수만 재사용한다.

입력:
  · 2.복지서비스 사업 현황 조사 목록(시군).xlsx
  · 3.복지서비스 사업 현황 조사 목록(기관).xlsx
  · 2.시군/**/*.hwpx
  · 3.유관기관/**/*.hwpx

출력:
  · 파싱 item: HWPX 표의 한 사업을 한글 필드명 dict로 표현
  · DB row: tbl_wlf_srvc 계열 테이블에 넣기 좋은 영문 컬럼명 dict
  · 단독 실행 시 result_v4_*.json, insert_v4_*.sql 파일

v5 변경:
  · 표는 마크다운이 아닌 HTML <table> 형식으로 출력
  · 주요 본문 DB 컬럼은 hp:p 단락마다 줄바꿈 보존

v6 변경:
  · HWPX header.xml의 스타일 참조를 읽어 HTML 표 스타일 일부 보존
    (테두리, 배경색, 셀 병합, 정렬, padding, 글자 크기/굵기/밑줄)

[새 DB 컬럼 (ALTER TABLE 필요)]
  REMARK       VARCHAR(500)   -- 비고  (시군만, 기관은 NULL)
  ITRST_TPC1   VARCHAR(100)   -- 관심분야1
  ITRST_TPC2   VARCHAR(100)   -- 관심분야2
"""

import xml.etree.ElementTree as ET
import json
import os
import glob
import zipfile
import re
import calendar
import uuid
import html
from datetime import datetime

try:
    import openpyxl
except ImportError:
    raise ImportError("openpyxl 이 필요합니다. 'uv add openpyxl' 로 설치하세요.")

# ──────────────────────────────────────────────────────────────
# 경로 상수
#
# 경로 기준은 이 파일이 있는 폴더이며, 폴더/엑셀 파일명은 현재 작업 구조에 맞춰져 있다.
# parse_hwpx_v6_db.py는 SCRIPT_DIR만 공유하고, 실제 입력 경로는 자체 설정값으로 넘긴다.
# ──────────────────────────────────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
SIGUN_DIR    = os.path.join(SCRIPT_DIR, "2.시군")
ORGAN_DIR    = os.path.join(SCRIPT_DIR, "3.유관기관")
EXCEL_SIGUN  = os.path.join(SCRIPT_DIR, "2.복지서비스 사업 현황 조사 목록(시군).xlsx")
EXCEL_ORGAN  = os.path.join(SCRIPT_DIR, "3.복지서비스 사업 현황 조사 목록(기관).xlsx")

_PARAGRAPH_LINE_FIELDS = {
    "근거", "목적", "지원내용(사업내용)", "신청방법", "제출서류(구비서류)",
    "지원대상(사업대상)", "시행주체",
}


def _preserve_paragraph_lines(field_name):
    # 본문성 필드는 문단 줄바꿈이 의미가 있으므로 공백으로 뭉개지 않는다.
    return field_name in _PARAGRAPH_LINE_FIELDS


def _join_field_parts(parts, field_name):
    # 같은 필드가 여러 문단/셀에 나뉘어 있을 때 필드 성격에 맞는 구분자로 합친다.
    sep = "\n" if _preserve_paragraph_lines(field_name) else " "
    return sep.join([p for p in parts if p]).strip()


def _normalize_field_text(text, preserve_lines=False):
    # HWPX에서 나온 공백/개행을 DB 저장에 적당한 형태로 정규화한다.
    text = str(text or "")
    if preserve_lines:
        lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in text.splitlines()]
        return "\n".join([line for line in lines if line]).strip()
    return re.sub(r'\s+', ' ', text).strip()


def _css_border(side_el):
    # header.xml의 borderFill 정보를 CSS border 문자열로 변환한다.
    # HWPX 단위는 mm에 가까운 문자열이라 대략 px로 환산한다.
    if side_el is None:
        return None
    border_type = side_el.get("type", "NONE")
    if border_type == "NONE":
        return "none"
    style = {
        "SOLID": "solid",
        "DASH": "dashed",
        "DOT": "dotted",
        "DOUBLE": "double",
        "DOUBLE_SLIM": "double",
        "THICK": "solid",
    }.get(border_type, "solid")
    width = side_el.get("width", "0.12 mm")
    m = re.search(r"([0-9.]+)", width)
    px = max(1, round(float(m.group(1)) * 3.78)) if m else 1
    return f"{px}px {style} {side_el.get('color', '#000000')}"


def _normalize_css_color(color):
    # HWPX 색상값은 RGB/ARGB/none 등으로 섞여 들어오므로 CSS에서 쓸 수 있는 RGB로 정리한다.
    # 흰색 배경은 굳이 inline style로 남기지 않는다.
    color = (color or "").strip()
    if not color or color.lower() == "none":
        return None

    if re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        color = f"#{color}"
    elif re.fullmatch(r"#[0-9A-Fa-f]{8}", color):
        # HWPX sometimes stores ARGB; CSS hex expects RGB here.
        color = f"#{color[-6:]}"
    elif re.fullmatch(r"[0-9A-Fa-f]{8}", color):
        color = f"#{color[-6:]}"

    if color.lower() in ("#ffffff", "#fff"):
        return None
    return color


def _find_first_by_local_name(root, local_name):
    # 네임스페이스가 다른 XML에서도 local name만 보고 첫 요소를 찾기 위한 작은 유틸.
    for el in root.iter():
        if el.tag.split("}")[-1] == local_name:
            return el
    return None


def _css_fill_background(root):
    # borderFill 내부의 fillBrush/winBrush에서 셀 배경색을 CSS로 뽑는다.
    fill = _find_first_by_local_name(root, "fillBrush")
    if fill is None:
        return None

    win_brush = _find_first_by_local_name(fill, "winBrush")
    if win_brush is None:
        return None

    color = _normalize_css_color(win_brush.get("faceColor"))
    return f"background-color:{color}" if color else None


def _build_hwpx_style_maps(header_root):
    # HWPX의 Contents/header.xml에는 표 테두리, 문단 정렬, 글자 속성 같은 스타일 정의가 있다.
    # section XML의 셀/문단/글자 run은 ID만 참조하므로, 먼저 ID -> CSS 문자열 맵을 만든다.
    ns_head = {
        "hh": "http://www.hancom.co.kr/hwpml/2011/head",
        "hc": "http://www.hancom.co.kr/hwpml/2011/core",
    }
    maps = {"border": {}, "para": {}, "char": {}}
    if header_root is None:
        return maps

    for border_fill in header_root.findall(".//hh:borderFill", ns_head):
        css = []
        for tag, css_name in [
            ("leftBorder", "border-left"),
            ("rightBorder", "border-right"),
            ("topBorder", "border-top"),
            ("bottomBorder", "border-bottom"),
        ]:
            val = _css_border(border_fill.find(f"hh:{tag}", ns_head))
            if val:
                css.append(f"{css_name}:{val}")
        fill_style = _css_fill_background(border_fill)
        if fill_style:
            css.append(fill_style)
        maps["border"][border_fill.get("id")] = ";".join(css)

    for para_pr in header_root.findall(".//hh:paraPr", ns_head):
        css = []
        align = para_pr.find("hh:align", ns_head)
        if align is not None and align.get("horizontal"):
            horizontal = align.get("horizontal").lower()
            css.append(f"text-align:{horizontal}")
        maps["para"][para_pr.get("id")] = ";".join(css)

    for char_pr in header_root.findall(".//hh:charPr", ns_head):
        css = []
        height = char_pr.get("height")
        if height:
            css.append(f"font-size:{max(10, round(int(height) / 115, 1))}pt")
        color = char_pr.get("textColor")
        if color and color.lower() != "#000000":
            css.append(f"color:{color}")
        if char_pr.find("hh:bold", ns_head) is not None:
            css.append("font-weight:bold")
        underline = char_pr.find("hh:underline", ns_head)
        if underline is not None and underline.get("type", "NONE") != "NONE":
            css.append("text-decoration:underline")
        maps["char"][char_pr.get("id")] = ";".join(css)

    return maps


def _cell_to_html_text(cell, ns, normalize_phone=False, style_maps=None):
    """HWPX 셀 내용을 HTML 셀 본문으로 변환한다.

    hp:p는 <p>로, br/lineBreak는 <br>로 보존한다.
    표 안에 다시 표가 들어 있는 경우 재귀적으로 HTML table을 만든다.
    """
    style_maps = style_maps or {"border": {}, "para": {}, "char": {}}
    paras = []
    direct_paras = cell.findall('./hp:subList/hp:p', ns)
    if not direct_paras:
        direct_paras = cell.findall('.//hp:p', ns)

    for p in direct_paras:
        pieces = []
        for run in p.findall('hp:run', ns):
            run_style = style_maps.get("char", {}).get(run.get("charPrIDRef"), "")
            text_parts = []
            for child in run:
                tag = child.tag.split('}')[-1]
                if tag == 't':
                    text_parts.append(child.text or "")
                elif tag in {"br", "lineBreak"}:
                    text_parts.append("\n")
                elif tag == "tbl":
                    nested = _tbl_to_html(child, ns, normalize_phone=normalize_phone, style_maps=style_maps)
                    if nested:
                        pieces.append(nested)
            txt = "".join(text_parts).strip()
            if normalize_phone:
                txt = _normalize_phone_cell(txt)
            if txt:
                escaped = html.escape(txt).replace("\n", "<br>")
                if run_style:
                    pieces.append(f'<span style="{run_style}">{escaped}</span>')
                else:
                    pieces.append(escaped)

        content = "".join(pieces).strip()
        if content:
            para_style = style_maps.get("para", {}).get(p.get("paraPrIDRef"), "")
            if para_style:
                paras.append(f'<p style="{para_style}">{content}</p>')
            else:
                paras.append(f"<p>{content}</p>")
    return "".join(paras)


def _tbl_to_html(tbl, ns, normalize_phone=False, style_maps=None):
    """HWPX 표 하나를 HTML <table> 문자열로 변환한다.

    rowSpan/colSpan, borderFill, vertical align, 문단/글자 스타일 일부를 보존한다.
    복지서비스 본문 컬럼에는 이 HTML이 그대로 들어갈 수 있다.
    """
    """HWPX 표를 HTML table 문자열로 변환한다."""
    style_maps = style_maps or {"border": {}, "para": {}, "char": {}}
    rows = tbl.findall('./hp:tr', ns)
    if not rows:
        return ''

    # 문의처 별첨에서 1행 다셀 구조가 단락별로 정렬된 경우,
    # v4의 행 확장 로직을 HTML 행으로 보존한다.
    if normalize_phone and len(rows) == 1:
        cells = rows[0].findall('./hp:tc', ns)
        if len(cells) > 1:
            cell_paras = [_get_cell_paras(c, ns) for c in cells]
            val_paras = [p for p in cell_paras[1:] if p]
            if (len(val_paras) >= 2
                    and len(set(len(p) for p in val_paras)) == 1
                    and len(val_paras[0]) > 1):
                html_lines = ['<table class="hwpx-table" style="border-collapse:collapse;table-layout:auto;width:auto">']
                for i in range(len(val_paras[0])):
                    html_lines.append('<tr>')
                    for col in val_paras:
                        content = html.escape(_normalize_phone_cell(col[i]))
                        html_lines.append(f'<td style="padding:4px 8px;white-space:nowrap">{content}</td>')
                    html_lines.append('</tr>')
                html_lines.append('</table>')
                return "\n".join(html_lines)

    table_style = "border-collapse:collapse;table-layout:auto;width:auto"
    html_lines = [f'<table class="hwpx-table" style="{table_style}">']
    for row in rows:
        html_lines.append('<tr>')
        for cell in row.findall('./hp:tc', ns):
            span_info = cell.find('./hp:cellSpan', ns)
            row_span = int(span_info.get('rowSpan', 1)) if span_info is not None else 1
            col_span = int(span_info.get('colSpan', 1)) if span_info is not None else 1
            attrs = []
            if row_span > 1:
                attrs.append(f'rowspan="{row_span}"')
            if col_span > 1:
                attrs.append(f'colspan="{col_span}"')
            styles = []
            border_style = style_maps.get("border", {}).get(cell.get("borderFillIDRef"), "")
            if border_style:
                styles.append(border_style)
            sub_list = cell.find('hp:subList', ns)
            if sub_list is not None and sub_list.get("vertAlign"):
                vert_align = sub_list.get("vertAlign").lower()
                if vert_align == "center":
                    vert_align = "middle"
                styles.append(f'vertical-align:{vert_align}')
            styles.append("padding:4px 8px")
            styles.append("white-space:nowrap")
            attrs.append(f'style="{";".join(styles)}"')
            attr_text = (" " + " ".join(attrs)) if attrs else ""
            html_lines.append(f'<td{attr_text}>{_cell_to_html_text(cell, ns, normalize_phone, style_maps)}</td>')
        html_lines.append('</tr>')
    html_lines.append('</table>')
    return "\n".join(html_lines)

def _folder_to_region(folder_name):
    # "01.창원시" 같은 폴더명에서 앞 번호/점을 떼고 지역명만 얻는다.
    return re.sub(r'^\d+\.', '', folder_name).strip()

# ──────────────────────────────────────────────────────────────
# 엑셀 매핑 로드
#
# HWPX 파일명(stem)을 key로 해서 비고/관심분야 메타데이터를 찾기 위한 맵을 만든다.
# process_sigun/process_organ에서 파싱 item마다 이 메타데이터를 붙인다.
# ──────────────────────────────────────────────────────────────

def _excel_file_key(value):
    # 엑셀에 경로나 확장자가 섞여 있어도 파일명 stem만 key로 사용한다.
    text = str(value or '').strip()
    if not text:
        return ''
    return os.path.splitext(os.path.basename(text))[0]


def load_excel_sigun(excel_path):
    """
    시군 엑셀을 읽어 파일명별 메타데이터 맵을 만든다.

    반환:
        {파일명_stem: {'remark': str, 'itrst_tpc1': str, 'itrst_tpc2': str}}

    엑셀 헤더:
        연번(A), 부서(B), 사업명(C), 비고(D),
        파일명(2025)(E), 파일명(2026)(F), 관심분야1(G), 관심분야2(H)

    2025/2026 파일명이 둘 다 있으면 같은 비고/관심분야를 각각의 key에 등록한다.
    """
    mapping = {}
    wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if r_idx == 1:          # 헤더 행 스킵
                continue
            if not row or len(row) < 6:
                continue
            remark    = str(row[3] or '').strip()   # D열
            fname_25  = _excel_file_key(row[4])      # E열
            fname_26  = _excel_file_key(row[5])      # F열
            tpc1      = str(row[6] or '').strip() if len(row) > 6 else ''  # G열
            tpc2      = str(row[7] or '').strip() if len(row) > 7 else ''  # H열

            meta = {'remark': remark, 'itrst_tpc1': tpc1, 'itrst_tpc2': tpc2}
            if fname_25:
                mapping[fname_25] = meta
            if fname_26:
                # 2026 파일도 같은 비고/관심분야 사용
                mapping[fname_26] = meta
    wb.close()
    return mapping


def load_excel_organ(excel_path):
    """
    유관기관 엑셀을 읽어 파일명별 메타데이터 맵을 만든다.

    반환:
        {파일명_stem: {'remark': '', 'itrst_tpc1': str, 'itrst_tpc2': str}}

    엑셀 헤더:
        연번(A), 시군(B), 기관명(C), 사업명(D), 관심분야1(E), 관심분야2(F), 파일명(G)

    기관 엑셀에는 비고 컬럼이 없으므로 remark는 빈 문자열로 둔다.
    """
    mapping = {}
    wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
    # 첫 번째 시트만 사용 (Sheet1 제외)
    ws = wb[wb.sheetnames[0]]
    for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if r_idx == 1:
            continue
        if not row or len(row) < 7:
            continue
        tpc1   = str(row[4] or '').strip() if len(row) > 4 else ''  # E열
        tpc2   = str(row[5] or '').strip() if len(row) > 5 else ''  # F열
        fname  = _excel_file_key(row[6]) if len(row) > 6 else ''    # G열
        if fname:
            mapping[fname] = {'remark': '', 'itrst_tpc1': tpc1, 'itrst_tpc2': tpc2}
    wb.close()
    return mapping

# ──────────────────────────────────────────────────────────────
# HWPX 파싱
#
# HWPX는 zip 안에 Contents/section*.xml이 있고, 그 안의 표(hp:tbl)에
# "사업명 / 생애주기 / 지원내용 ..." 같은 항목들이 들어 있다.
#
# parse_welfare_services()가 section XML 하나를 읽어 사업 단위 dict를 만들고,
# _parse_hwpx()가 HWPX zip 전체의 section들을 순회한다.
# ──────────────────────────────────────────────────────────────

def extract_cell_value(cell_or_cells, ns, field_name="", style_maps=None):
    """HWPX 표의 값 셀에서 텍스트/체크박스/중첩표를 추출한다.

    주요 처리:
    - 일반 텍스트는 공백/줄바꿈을 정리한다.
    - 체크박스가 있는 항목은 선택된 항목만 list로 만든다.
    - 문의처/본문 안의 표는 HTML table로 보존한다.
    - 본문성 필드는 문단 줄바꿈을 유지한다.
    """
    if not isinstance(cell_or_cells, list):
        cell_or_cells = [cell_or_cells]

    all_items = []
    for cell in cell_or_cells:
        cell_items = []
        elements_inside_tables = set()
        for tbl in cell.findall('.//hp:tbl', ns):
            for desc in tbl.iter():
                if desc is not tbl:
                    elements_inside_tables.add(desc)

        elements_inside_p = set()
        for child in cell.iter():
            if child in elements_inside_tables or child in elements_inside_p:
                continue
            tag_name = child.tag.split('}')[-1]

            if tag_name == 'p':
                p_items = []
                current_checkbox_state = None
                for el in child.iter():
                    if el is not child:
                        if el.tag.split('}')[-1] == 'tbl' or el in elements_inside_tables:
                            continue
                        elements_inside_p.add(el)
                    tag = el.tag.split('}')[-1]
                    if tag == 'checkBtn':
                        val = el.get('value')
                        current_checkbox_state = (val == 'CHECKED')
                    elif tag == 't':
                        text = "".join(el.itertext())
                        if text:
                            if current_checkbox_state is False:
                                preserved = "".join(c for c in text if c in [')', ']', '(', '['])
                                if preserved:
                                    p_items.append(preserved)
                            else:
                                p_items.append(text.strip())
                    elif tag in {"br", "lineBreak"}:
                        p_items.append("\n")
                p_text = _normalize_field_text("".join(p_items), _preserve_paragraph_lines(field_name))
                if p_text:
                    cell_items.append(p_text)

            elif tag_name == 'tbl':
                html_table = _tbl_to_html(child, ns, style_maps=style_maps)
                if html_table:
                    cell_items.append("[[HTML_TABLE]]\n" + html_table + "\n[[/HTML_TABLE]]")

        all_items.append(_join_field_parts(cell_items, field_name))

    final_string = _join_field_parts(all_items, field_name)
    if not final_string:
        return ""

    allowed_values = []
    if field_name == "생애주기":
        allowed_values = ["임신·출산", "영유아", "아동", "청소년", "청년", "중장년", "노년"]
    elif field_name == "가구상황":
        allowed_values = ["저소득", "장애인", "한부모·조손", "다자녀", "다문화·탈북민", "보훈"]
    elif field_name == "제공유형":
        allowed_values = ["현금", "현물", "서비스"]
    elif field_name == "지원대상(사업대상)":
        allowed_values = ["저소득", "중위소득 기준있음", "누구나"]
    elif field_name in ["시행주체", "신청기간"]:
        allowed_values = ["중앙부처", "광역", "시군", "공공기관", "민간",
                          "연중", "분기", "반기", "별도신청기간有", "기타"]
    elif field_name == "신청자격":
        allowed_values = ["개인", "법인, 단체, 기업 등", "기타"]

    if allowed_values:
        extracted_list = []
        for val in allowed_values:
            if val in final_string:
                extracted_list.append(val)

        if field_name in ["생애주기", "가구상황", "제공유형", "신청자격"]:
            if len(extracted_list) > 1:
                return extracted_list
            elif len(extracted_list) == 1:
                return extracted_list[0]
            else:
                return ""

    extracted_html_tables = []
    def tbl_replacer(match):
        extracted_html_tables.append(match.group(1))
        return f"__HTML_TABLE_{len(extracted_html_tables)-1}__"

    final_string = re.sub(r'\[\[HTML_TABLE\]\](.*?)\[\[/HTML_TABLE\]\]', tbl_replacer, final_string, flags=re.DOTALL)
    final_string = re.sub(r'\s{2,}(?=[()[\]])', ' ', final_string)
    final_string = re.sub(r'(?<=[()[\]])\s{2,}', ' ', final_string)

    if field_name in ["지원대상(사업대상)", "시행주체", "신청기간"] and allowed_values:
        extracted_list = [v for v in allowed_values if v in final_string]
        clean_text = final_string
        for val in extracted_list:
            clean_text = clean_text.replace(val, "")
        clean_text = re.sub(r'\(\s*\)', '', clean_text)
        clean_text = re.sub(r'\[\s*\]', '', clean_text)
        clean_text = re.sub(r' +', ' ', clean_text).strip()
        if clean_text.endswith('(') or clean_text.endswith('['):
            clean_text = clean_text[:-1].strip()

        result = []
        if clean_text:
            text_parts = [clean_text] if _preserve_paragraph_lines(field_name) else re.split(r'\s{2,}', clean_text)
            for part in text_parts:
                for i, tbl_content in enumerate(extracted_html_tables):
                    if f"__HTML_TABLE_{i}__" in part:
                        part = part.replace(f"__HTML_TABLE_{i}__", "\n" + tbl_content + "\n")
                result.append(_normalize_field_text(part, _preserve_paragraph_lines(field_name)))
        result.extend(extracted_list)
        return result

    parts = [final_string] if _preserve_paragraph_lines(field_name) else re.split(r'\s{2,}', final_string)
    restored_parts = []
    for part in parts:
        for i, tbl_content in enumerate(extracted_html_tables):
            if f"__HTML_TABLE_{i}__" in part:
                part = part.replace(f"__HTML_TABLE_{i}__", "\n" + tbl_content + "\n")
        restored_parts.append(_normalize_field_text(part, _preserve_paragraph_lines(field_name)))

    if len(restored_parts) > 1:
        return restored_parts
    return restored_parts[0] if restored_parts else ""


# ── 별첨 관련 패턴 ──────────────────────────────────────────────
_BYUL_PATTERN = re.compile(
    r'별\s*첨\s*\d*\s*참고|별\s*첨\s*\d*\s*참조|별도\s*자료\s*참고|별도\s*자료\s*참조|별\s*첨',
    re.IGNORECASE
)
# 0000 / ○○○○ 더미 전화번호 패턴 (0 또는 ○(U+25CB) 혼용 허용)
_ZO = r'[0○]'   # 숫자 0 또는 ○ 기호
_ZERO_PHONE_PATTERN = re.compile(
    rf'\d{{2,4}}-{_ZO}{{3,4}}-{_ZO}{{4}}'
    rf'|(?<!\d){_ZO}{{3,4}}-{_ZO}{{4}}(?!\d)'
    rf'|☎\s*{_ZO}{{4,}}'
)


def _is_byul_ref(text):
    """문의처 텍스트에 별첨 참조 문구 포함 여부"""
    return bool(_BYUL_PATTERN.search(str(text)))


def _has_zero_phone(text):
    """000-0000, 055-000-0000 등 0000 더미 번호 포함 여부"""
    return bool(_ZERO_PHONE_PATTERN.search(str(text)))


def _clean_inqpl(text):
    """INQPL 원문에서 별첨 참조 문구·0000 더미 번호·☎ 기호 등 정제"""
    if isinstance(text, list):
        text = ' '.join(str(t) for t in text)
    text = str(text)
    # 0000/○○○○ 더미 번호 제거
    text = re.sub(rf'\d{{2,4}}-{_ZO}{{3,4}}-{_ZO}{{4}}', '', text)
    text = re.sub(rf'(?<!\d){_ZO}{{3,4}}-{_ZO}{{4}}(?!\d)', '', text)
    text = re.sub(rf'☎\s*{_ZO}{{4,}}', '', text)
    # 별첨 참조 문구 제거
    text = _BYUL_PATTERN.sub('', text)
    # ☎ 기호 제거
    text = text.replace('☎', '')
    # 빈 괄호 제거
    text = re.sub(r'\(\s*\)', '', text)
    text = re.sub(r'\[\s*\]', '', text)
    # 줄바꿈은 보존하고 같은 줄 내 공백만 정리
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{2,}', '\n', text)
    text = text.strip()
    return text


def _normalize_phone_cell(txt):
    """셀 텍스트의 전화번호 정규화: 공백 제거, 국번없는 번호에 055 추가"""
    txt_nospace = re.sub(r'\s', '', txt)
    # 완전한 번호에서 공백 제거
    if re.search(r'\d{2,4}-\d{3,4}-\d{4}', txt_nospace):
        return re.sub(r'(\d{2,4})-\s*(\d{3,4})-\s*(\d{4})', r'\1-\2-\3', txt)
    # 국번없는 번호에 055 추가
    return re.sub(r'(?<!\d)(\d{3,4}-\d{4})(?!\d)', r'055-\1', txt)


def _get_cell_paras(cell, ns):
    """셀의 단락별 텍스트 목록 반환 (빈 단락 제외)"""
    paras = []
    for p in cell.findall('.//hp:p', ns):
        parts = [t.text.strip() for t in p.findall('.//hp:t', ns) if t.text and t.text.strip()]
        txt = ''.join(parts).strip()
        if txt:
            paras.append(txt)
    return paras


def _has_byul_table(tbl, ns):
    """별첨 테이블 여부: 전화번호(완전/단축) 또는 4자리 내선번호 패턴 포함 여부"""
    rows = tbl.findall('./hp:tr', ns)
    if not rows:
        return False
    for row in rows:
        for tc in row.findall('./hp:tc', ns):
            txt = ' '.join(t.text or '' for t in tc.findall('.//hp:t', ns) if t.text)
            txt_nospace = re.sub(r'\s', '', txt)
            if re.search(r'\d{2,4}-\d{3,4}-\d{4}', txt_nospace):
                return True
            if re.search(r'(?<!\d)\d{3,4}-\d{4}(?!\d)', txt):
                return True
            # 4자리 내선번호만 있는 경우 (예: "물금읍 7043")
            if re.search(r'(?<!\d)\d{4}(?!\d)', txt):
                return True
    return False


_PH_RE = re.compile(r'\d{2,4}-\d{3,4}-\d{4}|(?<!\d)\d{3,4}-\d{4}(?!\d)')


def _row_has_phone(cell_texts):
    """행의 셀 중 전화번호 패턴 포함 여부 (--- 생략 판단용)"""
    return any(_PH_RE.search(t) for t in cell_texts)


def _fix_alternating_phone_table(inqpl):
    """읍면동명행/4자리숫자행 교대 마크다운 테이블을 '읍면동 | 055-XXX-XXXX' 쌍으로 변환.
    텍스트에서 '055-XXX-○○○○' 패턴을 추출하여 지역번호로 사용하고 해당 문구는 제거한다."""
    if not inqpl or '|' not in str(inqpl):
        return inqpl
    text = str(inqpl)

    # 지역번호 추출 (예: 055-359)
    area_m = re.search(rf'(\d{{2,4}}-\d{{3,4}})-{_ZO}{{3,4}}', text)
    if not area_m:
        return inqpl
    area_code = area_m.group(1)

    # 테이블 앞 텍스트 / 테이블 행 분리
    lines = text.split('\n')
    pre_lines, tbl_lines = [], []
    in_tbl = False
    for line in lines:
        if line.strip().startswith('|'):
            in_tbl = True
        (tbl_lines if in_tbl else pre_lines).append(line)

    if not tbl_lines:
        return inqpl

    _d4 = re.compile(r'^\d{4}$')

    def parse_md_row(line):
        return [c.strip() for c in line.strip().strip('|').split('|')]

    data_rows = [parse_md_row(l) for l in tbl_lines if '---' not in l]

    if len(data_rows) < 2:
        return inqpl

    # 교대 패턴 감지: 짝수 인덱스=이름, 홀수 인덱스=4자리숫자
    is_alt = True
    for i, row in enumerate(data_rows):
        for cell in row:
            if not cell:
                continue
            if i % 2 == 0 and _d4.match(cell):   # 이름 행에 숫자만 있으면 아님
                is_alt = False; break
            if i % 2 == 1 and not _d4.match(cell): # 숫자 행에 이름이 있으면 아님
                is_alt = False; break
        if not is_alt:
            break

    if not is_alt:
        return inqpl

    # (읍면동, 전체번호) 쌍 생성
    pairs = []
    for i in range(0, len(data_rows) - 1, 2):
        name_row = data_rows[i]
        num_row  = data_rows[i + 1]
        for j in range(min(len(name_row), len(num_row))):
            name = name_row[j] if j < len(name_row) else ''
            num  = num_row[j]  if j < len(num_row)  else ''
            if name and num and _d4.match(num):
                pairs.append((name, f"{area_code}-{num}"))

    if not pairs:
        return inqpl

    # 새 마크다운 (전화번호가 모든 행에 있으므로 --- 없음)
    new_md = '\n'.join(f'| {n} | {p} |' for n, p in pairs)

    # prefix 텍스트: ○○○○/0000 포함 전화번호 문구 제거 후 정리
    prefix = '\n'.join(pre_lines)
    prefix = re.sub(rf'\s*\d{{2,4}}-\d{{3,4}}-{_ZO}{{3,4}}\s*', ' ', prefix)
    prefix = re.sub(rf'☎\s*{_ZO}+\s*', '', prefix)
    prefix = re.sub(r'[ \t]+', ' ', prefix).strip()

    return f"{prefix}\n{new_md}" if prefix else new_md


def _tbl_to_inqpl_html(tbl, ns, style_maps=None):
    """별첨 문의처 테이블을 HTML 형식으로 변환. 전화번호 정규화 포함."""
    return _tbl_to_html(tbl, ns, normalize_phone=True, style_maps=style_maps)


def parse_welfare_services(xml_source, welfare_year=None, region=None,
                            org_nm=None, file_dir=None, uuid_nm=None,
                            style_maps=None):
    """section XML 하나에서 복지서비스 사업 항목들을 추출한다.

    HWPX 본문에는 여러 hp:tbl이 들어 있고, 각 표가 사업 1건이거나
    앞 사업의 별첨 문의처 표일 수 있다. 이 함수는 표의 첫 열을 필드명으로 보고,
    유효한 필드명(사업명/지원내용/문의처 등)을 만나면 parsed_data에 쌓는다.

    반환 item은 아직 DB 컬럼명이 아니라 한글 필드명 dict다.
    build_db_rows()가 이 item들을 DB 컬럼 dict로 변환한다.
    """
    ns = {'hp': 'http://www.hancom.co.kr/hwpml/2011/paragraph'}
    try:
        tree = ET.parse(xml_source)
        root = tree.getroot()
    except Exception as e:
        print(f"  XML 파싱 오류: {e}")
        return []

    tables = root.findall('.//hp:tbl', ns)
    parsed_items = []
    # "문의처: 별첨 참고"처럼 본문 표에서 별첨을 가리키는 경우가 있다.
    # 뒤쪽에서 별첨 연락처 표가 발견되면 이 목록의 item들에 문의처 HTML을 채워 넣는다.
    pending_byul = []

    valid_fields = [
        "사업명", "생애주기", "가구상황", "근거", "목적",
        "제공유형", "지원내용(사업내용)", "지원대상(사업대상)",
        "시행주체", "신청기간", "신청자격", "신청방법",
        "제출서류(구비서류)", "문의처", "담당부서"
    ]

    for tbl in tables:
        # HWPX 안의 표는 사업 본문 표, 별첨 문의처 표, 본문 안 중첩표가 섞여 있다.
        # 여기서는 최상위 표를 순회하면서 유효 필드명이 있는 표만 사업으로 본다.
        rows = tbl.findall('./hp:tr', ns)
        if not rows:
            continue

        parsed_data = {}
        last_field_name = None

        for row in rows:
            cells = row.findall('./hp:tc', ns)
            if not cells:
                continue

            if len(cells) == 1:
                # 필드명 없이 값 셀만 이어지는 행은 이전 필드의 추가 설명으로 본다.
                additional_val = extract_cell_value(cells[0], ns, field_name=last_field_name, style_maps=style_maps)
                if last_field_name and additional_val:
                    existing = parsed_data[last_field_name]
                    if isinstance(existing, list) and isinstance(additional_val, list):
                        for val in additional_val:
                            if val not in existing:
                                existing.append(val)
                    elif isinstance(existing, list) and isinstance(additional_val, str):
                        if additional_val not in existing:
                            existing.append(additional_val)
                    elif isinstance(existing, str) and isinstance(additional_val, list):
                        parsed_data[last_field_name] = [existing] + [v for v in additional_val if v != existing]
                    else:
                        if additional_val not in existing:
                            sep = "\n" if _preserve_paragraph_lines(last_field_name) else " "
                            parsed_data[last_field_name] = existing + sep + additional_val
                continue

            field_name_texts = []
            # 첫 번째 셀에서 필드명을 뽑는다.
            # 공백/개행을 제거해 "지원내용 (사업내용)" 같은 흔들림을 줄인다.
            for p in cells[0].findall('.//hp:p', ns):
                for t in p.findall('.//hp:t', ns):
                    if t.text:
                        field_name_texts.append(t.text.strip())

            field_name = "".join(field_name_texts).replace(" ", "").replace("\n", "")
            is_valid_field = False
            for vf in valid_fields:
                if field_name and (field_name == vf or field_name.replace(" ", "") == vf.replace(" ", "")):
                    is_valid_field = True
                    field_name = vf
                    break

            if not is_valid_field or len(field_name) > 30 or (len(cells) > 1 and not field_name):
                # 첫 셀이 필드명이 아니면 이전 필드의 계속 내용으로 처리한다.
                additional_val = extract_cell_value(cells, ns, field_name=last_field_name, style_maps=style_maps)
                if last_field_name and additional_val:
                    existing = parsed_data[last_field_name]
                    if isinstance(existing, list) and isinstance(additional_val, list):
                        for val in additional_val:
                            if val not in existing:
                                existing.append(val)
                    elif isinstance(existing, list) and isinstance(additional_val, str):
                        if additional_val not in existing:
                            existing.append(additional_val)
                    elif isinstance(existing, str) and isinstance(additional_val, list):
                        parsed_data[last_field_name] = [existing] + [v for v in additional_val if v != existing]
                    else:
                        if additional_val not in existing:
                            sep = "\n" if _preserve_paragraph_lines(last_field_name) else " "
                            parsed_data[last_field_name] = existing + sep + additional_val
                continue

            # 문의처 행: 여러 값 셀의 단락이 정렬되면 행별로 쌍 결합
            if field_name == "문의처" and len(cells) > 2:
                val_para_lists = [_get_cell_paras(c, ns) for c in cells[1:]]
                non_empty = [p for p in val_para_lists if p]
                if (len(non_empty) >= 2
                        and len(set(len(p) for p in non_empty)) == 1
                        and len(non_empty[0]) > 1):
                    paired = [
                        ' '.join(non_empty[j][i] for j in range(len(non_empty)))
                        for i in range(len(non_empty[0]))
                    ]
                    field_val = '\n'.join(paired)
                else:
                    field_val = extract_cell_value(cells[1:], ns, field_name=field_name, style_maps=style_maps)
            else:
                field_val = extract_cell_value(cells[1:], ns, field_name=field_name, style_maps=style_maps)

            if field_name:
                # 사업명 앞의 ○ 접두사 제거
                if field_name == "사업명" and isinstance(field_val, str):
                    field_val = re.sub(r'^[○◎●\s]+', '', field_val).strip()
                parsed_data[field_name] = field_val
                last_field_name = field_name

        keys = list(parsed_data.keys())
        if keys and "사업명" in keys[0] and "담당부서" in keys[-1]:
            # 담당부서 → 전화번호 분리 (str / list 모두 처리)
            dept_val = parsed_data.get("담당부서", "")
            phone_num = ""
            if isinstance(dept_val, str) and "☎" in dept_val:
                parts = dept_val.split("☎", 1)
                parsed_data["담당부서"] = parts[0].strip()
                phone_num = parts[1].strip()
            elif isinstance(dept_val, list):
                clean_parts, phone_parts = [], []
                for v in dept_val:
                    sv = str(v)
                    if "☎" in sv:
                        sp = sv.split("☎", 1)
                        if sp[0].strip():
                            clean_parts.append(sp[0].strip())
                        phone_parts.append(sp[1].strip())
                    else:
                        clean_parts.append(sv)
                if phone_parts:
                    phone_num = " ".join(phone_parts)
                parsed_data["담당부서"] = " ".join(clean_parts) if clean_parts else ""

            # 문의처 정리 (☎ 등)
            inqpl_val = parsed_data.get("문의처", "")
            if isinstance(inqpl_val, str):
                parsed_data["문의처"] = inqpl_val
            elif isinstance(inqpl_val, list):
                parsed_data["문의처"] = ' '.join(str(v) for v in inqpl_val)

            final_data = {}
            if welfare_year:
                final_data["복지연도"] = welfare_year
            if region:
                final_data["시군"] = region
            if org_nm:
                final_data["ORG_NM"] = org_nm
            if file_dir:
                final_data["FILE_DIR"] = file_dir
            if uuid_nm:
                final_data["UUID_NM"] = uuid_nm

            for k, v in parsed_data.items():
                if k == "담당부서" and phone_num:
                    final_data[k] = v
                    final_data["전화번호"] = phone_num
                else:
                    final_data[k] = v

            # 별첨 참조 or 0000 번호 있으면 별첨 테이블 탐색 대상
            inqpl_str = str(final_data.get("문의처", ""))
            if _is_byul_ref(inqpl_str) or _has_zero_phone(inqpl_str):
                pending_byul.append(final_data)

            parsed_items.append(final_data)
            continue  # 복지 테이블은 아래 별첨 처리 건너뜀

        # ── 비복지 테이블: 별첨 테이블 여부 확인 ──────────────────
        if pending_byul and _has_byul_table(tbl, ns):
            html_table = _tbl_to_inqpl_html(tbl, ns, style_maps=style_maps)
            if html_table:
                for item in pending_byul:
                    prefix = _clean_inqpl(item.get("문의처", ""))
                    item["문의처"] = f"{prefix}\n{html_table}" if prefix else html_table
                pending_byul = []

    return parsed_items

# ──────────────────────────────────────────────────────────────
# DB 매핑 공통 상수 및 헬퍼
# ──────────────────────────────────────────────────────────────

_LFTM_CYCL_MAP  = {"임신·출산":0,"영유아":1,"아동":2,"청소년":3,"청년":4,"중장년":5,"노년":6}
_HSHD_STTN_MAP  = {"저소득":0,"장애인":1,"한부모·조손":2,"다자녀":3,"다문화·탈북민":4,"보훈":5}
_PVSN_TYPE_MAP  = {"현금":0,"현물":1,"서비스":2}
_SPRT_TRGT_MAP  = {"저소득":0,"중위소득 기준있음":1,"누구나":2}
_ENFC_MNBD_MAP  = {"중앙부처":0,"광역":1,"시군":2,"공공기관":3,"민간":4}
_APLY_PRD_TYPE_MAP = {"연중":0,"분기":1,"반기":2,"별도신청기간有":3,"기타":4}
_APLY_QLFC_MAP  = {"개인":0,"법인, 단체, 기업 등":1,"기타":2}


def _get_bitmap(item_val, mapping_dict, length):
    bits = ['0'] * length
    if not item_val:
        return "".join(bits)
    if isinstance(item_val, str):
        item_val = [item_val]
    for val in item_val:
        if val in mapping_dict:
            bits[mapping_dict[val]] = '1'
    return "".join(bits)


def _get_comma_map(item_val, mapping_dict):
    if not item_val:
        codes = [f"0000{val+1}" for val in mapping_dict.values()]
        return ",".join(codes)
    if isinstance(item_val, str):
        item_val = [item_val]
    codes = []
    for val in item_val:
        if val in mapping_dict:
            codes.append(f"0000{mapping_dict[val]+1}")
    return ",".join(codes)


def _extract_dates(text, default_year):
    """신청기간 텍스트에서 (시작일, 종료일) 쌍 목록을 반환.
    복수 기간(공공일자리 상/하반기, 추가모집 등)은 여러 쌍으로 분리된다.
    날짜 쌍 탐지: 연속된 두 날짜 사이에 '~' or '∼' 가 있으면 범위, 없으면 독립 기간."""
    if not text:
        return [("NULL", "NULL")]

    clean = str(text)
    if "<" in clean and ">" in clean:
        # v6 스타일 HTML의 태그/속성 안 숫자가 날짜로 오인되지 않도록
        # 날짜 추출 전 화면에 보이는 텍스트만 남긴다.
        clean = re.sub(r'<[^>]+>', ' ', clean)
        clean = html.unescape(clean)
    clean = re.sub(r'\s+', ' ', clean).strip()

    # 1) 연도 범위 패턴 (2023~2026년)
    m = re.search(r'(20\d{2})\s*[~∼\-]\s*(20\d{2})\s*년', clean)
    if m:
        y1, y2 = m.group(1), m.group(2)
        return [(f"'{y1}01010000'", f"'{y2}12310000'")]

    # 2) 상/하반기만 있고 날짜가 없으면 NULL
    if re.search(r"(?:^|[^\d])(\d{2,4})[.년]\s*(상|하)", clean):
        if not re.search(r'\d{1,2}\s*[월.]\s*\d{1,2}', clean):
            return [("NULL", "NULL")]

    # 3) 노이즈 제거
    for nw in ['상반기','하반기','상반','하반','신청기간내','신청기간 내',
               '년중','년 중','년도내','년내','기간내','년 신청기간']:
        clean = clean.replace(nw, ' ')
    # 차수 표기 제거 (1차, 2차, ... → 공백)
    clean = re.sub(r'\d+차', ' ', clean)
    # 주차/붙임 번호 제거 (1주차, 붙임2 등 날짜 오인식 방지)
    clean = re.sub(r'\d+\s*주차', ' ', clean)
    clean = re.sub(r'붙임\s*\d+', ' ', clean)
    # 'N월말' 표현 제거 (말일 설명 문구 — 별도 기간으로 오인식 방지)
    clean = re.sub(r'\d{1,2}월말', ' ', clean)
    # 4.29. ~ 30. 처럼 종료 월이 생략된 같은 달 범위 보정
    clean = re.sub(
        r'(\d{1,2})[월.]\s*(\d{1,2})[일.]?\s*([~∼])\s*(\d{1,2})(?!\s*[월.]\s*\d)(?=\s*[일.)\]])',
        r'\1.\2 \3 \1.\4',
        clean,
    )
    # 시간 표현 제거 (09:00, 16:30 등 → 월로 오인식 방지)
    clean = re.sub(r'\d{1,2}:\d{2}', ' ', clean)
    # 기간/조건 표현 제거 (1년 이내, 6개월 이상 등 자격조건 텍스트의 숫자 오인식 방지)
    clean = re.sub(r'\d+\s*(년|개월)\s*(이내|이상|이전|이후|경과)', ' ', clean)
    # 금액 표현 제거 (8천만원, 5만원 등) — \b 미적용, 한글 단위어 lookahead
    clean = re.sub(r'\d[\d,]*\s*(?:천만|억만|천억|천만원|억원|만원|천원|천만|억|만|천)(?!\d)', ' ', clean)
    # 수량 표현 제거 (1주택, 2가구 등)
    clean = re.sub(r'\d+\s*(?:주택|가구|명|회|개)(?!\w)', ' ', clean)

    # 4) 단독 4자리 연도 제거. 뒤에 월/일 숫자가 이어지면 보존한다.
    clean = re.sub(r'(20\d{2})[년.](?!\s*\d)', ' ', clean)
    if not re.search(r'\d', clean):
        return [("NULL", "NULL")]

    # ── 날짜 매치 목록 생성 (위치 정보 포함) ─────────────────────
    pattern = r'(?:(\d{4}|\d{2})[년.\-\s]+)?(\d{1,2})[월.\-\s]*(?:(\d{1,2})[일.\-\s]*|(중|中|경))?'
    date_matches = list(re.finditer(pattern, clean))

    last_y_full = "20" + default_year[-2:] if len(default_year) >= 2 else "2025"
    parsed = []  # list of dict with type / dt / y / m / pos_start / pos_end

    for obj in date_matches:
        y_raw, month_raw, d_raw, middle_raw = obj.groups()
        y, month, d, middle = y_raw, month_raw, d_raw, middle_raw

        # 2자리 연도가 12 이하면 year/month 스왑 후보
        if y and not d and len(y) == 2:
            try:
                if int(y) <= 12:
                    d = month; month = y; y = None
            except ValueError:
                pass

        # 연도 결정
        if y:
            cur_y = "20" + y if len(y) == 2 else y
            last_y_full = cur_y
        else:
            cur_y = last_y_full

        # 월 검증
        try:
            mo = int(month)
            if not (1 <= mo <= 12):
                continue
        except (ValueError, TypeError):
            continue

        # 일 검증
        if d:
            try:
                if not (1 <= int(d) <= 31):
                    continue
            except (ValueError, TypeError):
                continue

        if d:
            parsed.append({'type': 'normal',
                           'dt': f"{cur_y}{mo:02d}{int(d):02d}0000",
                           'y': cur_y, 'm': str(mo), 'd': str(int(d)),
                           'pos_s': obj.start(), 'pos_e': obj.end()})
        elif middle:
            parsed.append({'type': 'middle',
                           'dt': f"{cur_y}{mo:02d}010000",
                           'pos_s': obj.start(), 'pos_e': obj.end()})
        else:
            parsed.append({'type': 'month_only', 'y': cur_y, 'm': str(mo),
                           'pos_s': obj.start(), 'pos_e': obj.end()})

    if not parsed:
        return [("NULL", "NULL")]

    # ── 헬퍼 ─────────────────────────────────────────────────────
    def to_start(dt):
        if dt['type'] == 'month_only':
            return f"'{dt['y']}{int(dt['m']):02d}010000'"
        return f"'{dt['dt']}'"

    def to_end(dt, year_override=None):
        if dt['type'] == 'month_only':
            y2, m2 = int(year_override or dt['y']), int(dt['m'])
            try: ld = calendar.monthrange(y2, m2)[1]
            except: ld = 28
            return f"'{y2}{m2:02d}{ld:02d}0000'"
        if year_override and dt['type'] == 'normal':
            return f"'{int(year_override)}{int(dt['m']):02d}{int(dt['d']):02d}0000'"
        return f"'{dt['dt']}'"

    # ── 날짜 쌍 그룹화 (두 날짜 사이에 ~ 가 있으면 범위) ─────────
    pairs = []
    i = 0
    while i < len(parsed):
        d1 = parsed[i]
        if d1['type'] == 'middle':
            # 中 날짜는 항상 독립 기간 시작 (종료 없음)
            pairs.append((to_start(d1), "NULL"))
            i += 1
            continue

        if i + 1 < len(parsed):
            d2 = parsed[i + 1]
            between = clean[d1['pos_e']:d2['pos_s']]
            if re.search(r'[~∼]', between) and d2['type'] != 'middle':
                # 범위 쌍
                start_sql = to_start(d1)
                end_sql = to_end(d2)
                if start_sql != "NULL" and end_sql != "NULL" and end_sql < start_sql:
                    if re.search(r'그\s*다음\s*해|다음\s*해|익년|내년', between):
                        end_sql = to_end(d2, int(d1.get('y', default_year)) + 1)
                    elif (d1.get('y') == d2.get('y') and d1.get('m') == d2.get('m')
                          and d1.get('d') and d2.get('d')
                          and int(d2['d']) < int(d1['d']) and int(d2['d']) <= 9):
                        fixed_day = int(d2['d']) * 10
                        if int(d1['d']) <= fixed_day <= 31:
                            end_sql = f"'{int(d2['y'])}{int(d2['m']):02d}{fixed_day:02d}0000'"
                    if end_sql < start_sql:
                        end_sql = "NULL"
                pairs.append((start_sql, end_sql))
                i += 2
                continue

        # 독립 기간: month_only는 해당 월 전체(1일~말일), 나머지는 시작일만
        if d1['type'] == 'month_only':
            pairs.append((to_start(d1), to_end(d1)))
        else:
            pairs.append((to_start(d1), "NULL"))
        i += 1

    if not pairs:
        return [("NULL", "NULL")]

    # HTML 표나 모집회차 표에서 같은 날짜가 반복 추출될 수 있어 중복 제거.
    deduped = []
    seen = set()
    for pair in pairs:
        if pair not in seen:
            seen.add(pair)
            deduped.append(pair)
    pairs = deduped

    # APLY_BGNG_DT/APLY_END_DT 컬럼은 상세 모집표 전체를 표현하기 어렵다.
    # 지나치게 많은 기간은 row 폭증을 막기 위해 전체 범위 1건으로 축약한다.
    if len(pairs) > 24:
        starts = [s for s, _ in pairs if s != "NULL"]
        ends = [e for _, e in pairs if e != "NULL"]
        if starts:
            return [(min(starts), max(ends) if ends else "NULL")]
        return [("NULL", "NULL")]

    return pairs


def _separate_composite(item, field_name, bitmap_map, bit_len, is_single_code=False):
    val = item.get(field_name, [])
    text_desc_arr, checked_arr = [], []
    if isinstance(val, list):
        for v in val:
            if v in bitmap_map:
                checked_arr.append(v)
            else:
                text_desc_arr.append(v)
    elif isinstance(val, str):
        text_desc_arr.append(val)
    if is_single_code:
        # 복수 체크 허용: 쉼표 구분 코드 (예: "1,4")
        bit_str = ",".join(str(bitmap_map[v] + 1) for v in checked_arr) if checked_arr else ""
    else:
        bits = ['0'] * bit_len
        for v in checked_arr:
            if v in bitmap_map:
                bits[bitmap_map[v]] = '1'
        bit_str = "".join(bits)
    return bit_str, "\n".join(text_desc_arr)


def extract_telno(raw):
    """전화번호 문자열에서 번호 추출. 복수인 경우 쉼표로 구분.
    문서의 하이픈은 보존하고, 하이픈이 없으면 자리수 기준으로 보정한다."""
    if not raw:
        return ''

    def format_phone(token):
        token = re.sub(r'\s*-\s*', '-', str(token).strip())
        digits = re.sub(r'\D', '', token)
        if not (7 <= len(digits) <= 11):
            return None, None

        # 원문에 하이픈이 있으면 하이픈 구조를 유지한다.
        if '-' in token:
            return token, digits

        if len(digits) == 11:
            return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}", digits
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}", digits
        if len(digits) == 8:
            return f"{digits[:4]}-{digits[4:]}", digits
        if len(digits) == 7:
            return f"{digits[:3]}-{digits[3:]}", digits
        return digits, digits

    cleaned = str(raw).replace('☎', ' ')
    pattern = re.compile(
        r'(?<!\d)(?:'
        r'\d{2,4}\s*-\s*\d{3,4}\s*-\s*\d{4}'
        r'|\d{3,4}\s*-\s*\d{4}'
        r'|\d{7,11}'
        r')(?!\d)'
    )
    seen = set()
    phones = []
    for match in pattern.finditer(cleaned):
        phone, digits = format_phone(match.group(0))
        if phone and digits not in seen:
            seen.add(digits)
            phones.append(phone)
    return ','.join(phones)


# ──────────────────────────────────────────────────────────────
# DB 행 빌드 (파싱 결과 → DB 컬럼 dict 목록)
#
# parse_welfare_services()의 결과는 한글 필드명 중심이다.
# 이 단계에서 운영 DB 코드값/날짜/전화번호/관심분야/파일 메타데이터를 정리해
# parse_hwpx_v6_db.py가 바로 INSERT할 수 있는 컬럼명 dict로 바꾼다.
# ──────────────────────────────────────────────────────────────

def build_db_rows(parsed_items):
    """파싱 item list를 DB 컬럼 형식의 dict list로 변환한다.

    주요 변환:
    - 체크박스성 값은 코드 문자열/비트맵 문자열로 변환한다.
    - 신청기간에서 날짜를 추출한다.
    - 신청기간 날짜가 여러 개면 같은 사업명을 suffix로 나눠 여러 row를 만든다.
    - 문의처/전화번호는 DB 저장용으로 정리한다.
    - ORG_NM, FILE_DIR, UUID_NM, REMARK, ITRST_TPC1/2 같은 메타데이터를 유지한다.
    """
    db_rows = []

    for item in parsed_items:
        wlf_yr       = item.get("복지연도", "")
        wlf_srvc_nm  = item.get("사업명", "")
        lftm_cycl_cd = _get_comma_map(item.get("생애주기",""), _LFTM_CYCL_MAP)
        hshd_sttn_cd = _get_comma_map(item.get("가구상황",""), _HSHD_STTN_MAP)

        sigun_nm = item.get("시군","")
        # DB 직접 적재(parse_hwpx_v6_db.py)에서는 SIGUN_NM을 다시 tb_cd 기준 SIGUN_CD로 바꾼다.
        # 단독 SQL 출력(generate_sql_from_rows)에서는 이 전체 명칭을 tb_cd 서브쿼리에 사용한다.
        if sigun_nm == "경상남도":
            sigun_nm_full = "경상남도"
        elif sigun_nm:
            sigun_nm_full = f"경상남도 {sigun_nm}"
        else:
            sigun_nm_full = ""

        bss       = item.get("근거","")
        prps      = item.get("목적","")
        pvsn_type = _get_bitmap(item.get("제공유형",""), _PVSN_TYPE_MAP, 3)
        sprt_cn   = item.get("지원내용(사업내용)","")

        sprt_trgt,  sprt_trgt_cn  = _separate_composite(item,"지원대상(사업대상)",_SPRT_TRGT_MAP,3)
        enfc_mnbd,  enfc_mnbd_cn  = _separate_composite(item,"시행주체",_ENFC_MNBD_MAP,5)
        enfc_mnbd_cn = enfc_mnbd_cn.replace("○","").strip()
        aply_prd_type, aply_prd_text = _separate_composite(item,"신청기간",_APLY_PRD_TYPE_MAP,5,is_single_code=True)

        # 신청기간 문장에서 날짜 범위를 추출한다.
        # 날짜 범위가 여러 개면 아래 loop에서 같은 사업을 여러 row로 분리한다.
        aply_dates_list = _extract_dates(aply_prd_text, default_year=wlf_yr if wlf_yr else "2025")
        aply_qlfc   = _get_bitmap(item.get("신청자격",""), _APLY_QLFC_MAP, 3)
        aply_mthd   = item.get("신청방법","")
        sbmsn_dcmnt = item.get("제출서류(구비서류)","")
        inqpl_raw   = item.get("문의처","")
        if inqpl_raw and '|' in str(inqpl_raw):
            # 문의처에 HTML/마크다운 표가 섞인 경우, 표 앞의 일반 문장만 정리하고 표는 보존한다.
            # 테이블 앞 텍스트만 _clean_inqpl 적용 (○○○○ 등 제거)
            raw_str = str(inqpl_raw)
            split_idx = next((i for i, l in enumerate(raw_str.split('\n')) if l.strip().startswith('|')), None)
            if split_idx is not None:
                pre_lines = raw_str.split('\n')[:split_idx]
                tbl_part  = '\n'.join(raw_str.split('\n')[split_idx:])
                pre_clean = _clean_inqpl('\n'.join(pre_lines)) if pre_lines else ''
                inqpl = f"{pre_clean}\n{tbl_part}" if pre_clean else tbl_part
            else:
                inqpl = raw_str
            inqpl = _fix_alternating_phone_table(inqpl)
        else:
            inqpl = _clean_inqpl(inqpl_raw)
        tkcg_dept   = item.get("담당부서","")
        telno       = extract_telno(item.get("전화번호",""))
        remark      = item.get("REMARK","")
        itrst_tpc1  = item.get("ITRST_TPC1","")
        itrst_tpc2  = item.get("ITRST_TPC2","")
        org_nm      = item.get("ORG_NM","")
        file_dir    = item.get("FILE_DIR","")
        uuid_nm     = item.get("UUID_NM","")

        # 날짜는 SQL용 따옴표 포함 형식('202501010000'), JSON용은 따옴표 제거
        def _dt_val(dt_sql):
            """'202501010000' → '202501010000' (SQL 리터럴 그대로), NULL → None"""
            if dt_sql == "NULL":
                return None
            return dt_sql.strip("'")

        for idx, (aply_bgng_dt_sql, aply_end_dt_sql) in enumerate(aply_dates_list):
            # 신청기간이 여러 기간으로 나뉘면 WLF_SRVC_NM에 _2, _3 suffix를 붙여 별도 row로 만든다.
            cur_nm = wlf_srvc_nm if idx == 0 else f"{wlf_srvc_nm}_{idx+1}"
            db_rows.append({
                # DB 컬럼명으로 저장
                "WLF_YR":        wlf_yr,
                "WLF_SRVC_NM":   cur_nm,
                "LFTM_CYCL_CD":  lftm_cycl_cd,
                "HSHD_STTN_CD":  hshd_sttn_cd,
                "SIGUN_NM":      sigun_nm_full,   # SQL에서는 subquery로 치환
                "BSS":           bss,
                "PRPS":          prps,
                "PVSN_TYPE":     pvsn_type,
                "SPRT_CN":       sprt_cn,
                "SPRT_TRGT":     sprt_trgt,
                "SPRT_TRGT_CN":  sprt_trgt_cn,
                "ENFC_MNBD":     enfc_mnbd,
                "ENFC_MNBD_CN":  enfc_mnbd_cn,
                "APLY_PRD_TYPE": aply_prd_type if aply_prd_type else None,
                "APLY_BGNG_DT":  _dt_val(aply_bgng_dt_sql),
                "APLY_END_DT":   _dt_val(aply_end_dt_sql),
                "APLY_QLFC":     aply_qlfc,
                "APLY_MTHD":     aply_mthd,
                "SBMSN_DCMNT":   sbmsn_dcmnt,
                "INQPL":         inqpl,
                "TKCG_DEPT":     tkcg_dept,
                "TELNO":         telno,
                "REMARK":        remark if remark else None,
                "ITRST_TPC1":    itrst_tpc1 if itrst_tpc1 else None,
                "ITRST_TPC2":    itrst_tpc2 if itrst_tpc2 else None,
                "ORG_NM":        org_nm,
                "FILE_DIR":      file_dir,
                "UUID_NM":       uuid_nm,
            })

    return db_rows


# ──────────────────────────────────────────────────────────────
# SQL 생성 (DB rows → INSERT 구문)
#
# 이 함수는 이 파일을 단독 실행해서 insert_v4_*.sql 파일을 만들 때만 사용한다.
# 현재 DB 직접 적재 방식(parse_hwpx_v6_db.py)은 generate_sql_from_rows()를 쓰지 않고,
# build_db_rows() 결과를 파라미터 바인딩으로 바로 INSERT한다.
# ──────────────────────────────────────────────────────────────

def generate_sql_from_rows(db_rows):
    """DB row dict 목록을 INSERT SQL 문자열 목록으로 변환한다.

    보존 목적의 구버전 출력 경로다. 실제 최신 적재는 parse_hwpx_v6_db.py를 우선 사용한다.
    """
    def esc(txt):
        if txt is None:
            return 'NULL'
        if isinstance(txt, list):
            txt = "\n".join([str(t) for t in txt])
        if not txt:
            return "''"
        return "'" + str(txt).replace("'", "''") + "'"

    sql_statements = []
    for row in db_rows:
        sigun_nm = row["SIGUN_NM"]
        if sigun_nm == "경상남도":
            sigun_cd_query = "(select CD from tb_cd where CTGRY='4800000000' and CD_NM='경상남도')"
        elif sigun_nm:
            sigun_cd_query = f"(select CD from tb_cd where CTGRY='4800000000' and CD_NM='{sigun_nm}')"
        else:
            sigun_cd_query = "(select CD from tb_cd where CTGRY='0000000000' and CD_NM='국민건강보험공단')"

        aply_bgng_dt = f"'{row['APLY_BGNG_DT']}'" if row['APLY_BGNG_DT'] else "NULL"
        aply_end_dt  = f"'{row['APLY_END_DT']}'"  if row['APLY_END_DT']  else "NULL"
        aply_prd_type_sql = "NULL" if row['APLY_PRD_TYPE'] is None else esc(row['APLY_PRD_TYPE'])

        sql = f"""INSERT INTO tbl_wlf_srvc_new (
    WLF_YR, WLF_SRVC_NM, LFTM_CYCL_CD, HSHD_STTN_CD, SIGUN_CD,
    BSS, PRPS, PVSN_TYPE, SPRT_CN, SPRT_TRGT, SPRT_TRGT_CN,
    ENFC_MNBD, ENFC_MNBD_CN, APLY_PRD_TYPE, APLY_BGNG_DT,
    APLY_END_DT, APLY_QLFC, APLY_MTHD, SBMSN_DCMNT, INQPL,
    TKCG_DEPT, TELNO, WLF_SRVC_APLY_FORM_SN, RGTR_SN, RGTR_ID,
    RGTR_NM, RGTR_IP_ADDR, RGTR_BRWSR, REG_DT, MDFR_SN,
    MDFR_ID, MDFR_NM, MDFR_IP_ADDR, MDFR_BRWSR, MDFCN_DT,
    USE_YN, APLY_YN, INQ_CNT, ORG_NM, FILE_DIR, UUID_NM,
    REMARK, ITRST_TPC1, ITRST_TPC2
) VALUES (
    {esc(row['WLF_YR'])}, {esc(row['WLF_SRVC_NM'])}, {esc(row['LFTM_CYCL_CD'])}, {esc(row['HSHD_STTN_CD'])}, {sigun_cd_query},
    {esc(row['BSS'])}, {esc(row['PRPS'])}, {esc(row['PVSN_TYPE'])}, {esc(row['SPRT_CN'])}, {esc(row['SPRT_TRGT'])}, {esc(row['SPRT_TRGT_CN'])},
    {esc(row['ENFC_MNBD'])}, {esc(row['ENFC_MNBD_CN'])}, {aply_prd_type_sql}, {aply_bgng_dt},
    {aply_end_dt}, {esc(row['APLY_QLFC'])}, {esc(row['APLY_MTHD'])}, {esc(row['SBMSN_DCMNT'])}, {esc(row['INQPL'])},
    {esc(row['TKCG_DEPT'])}, {esc(row['TELNO'])}, 13, 0, 'F6EDF32073E0B6C7D5C4ACD8C7749C39',
    '25EF4517089D7E4FB3D19B74BB029AF2', '0:0:0:0:0:0:0:1', 'Mozilla/5.0', NOW(), 0,
    'F6EDF32073E0B6C7D5C4ACD8C7749C39', 'F6EDF32073E0B6C7D5C4ACD8C7749C39', '0:0:0:0:0:0:0:1', 'Mozilla/5.0', NOW(),
    'Y', 'Y', 0, {esc(row['ORG_NM'])}, {esc(row['FILE_DIR'])}, {esc(row['UUID_NM'])},
    {esc(row['REMARK'])}, {esc(row['ITRST_TPC1'])}, {esc(row['ITRST_TPC2'])}
);"""
        sql_statements.append(sql)

    return sql_statements

# ──────────────────────────────────────────────────────────────
# 파일 처리
#
# process_sigun/process_organ은 폴더 구조를 순회하면서 _parse_hwpx()를 호출한다.
# parse_hwpx_v6_db.py도 이 두 함수를 import해 같은 파싱 결과를 DB에 직접 넣는다.
# ──────────────────────────────────────────────────────────────

def _parse_hwpx(hwpx_path, welfare_year, region, org_nm, file_dir, uuid_nm):
    """단일 .hwpx 파일을 파싱해 한글 필드명 item list를 반환한다.

    HWPX는 zip 파일이므로 Contents/header.xml에서 스타일 맵을 먼저 읽고,
    Contents/section*.xml을 순회하면서 parse_welfare_services()로 실제 사업 내용을 추출한다.
    """
    items = []
    try:
        with zipfile.ZipFile(hwpx_path, 'r') as zf:
            style_maps = None
            if "Contents/header.xml" in zf.namelist():
                try:
                    with zf.open("Contents/header.xml") as header_file:
                        style_maps = _build_hwpx_style_maps(ET.parse(header_file).getroot())
                except Exception as e:
                    print(f"  스타일 파싱 실패 {os.path.basename(hwpx_path)}: {e}")

            section_files = sorted(
                n for n in zf.namelist()
                if n.startswith('Contents/section') and n.endswith('.xml')
            )
            for sec in section_files:
                with zf.open(sec) as f:
                    parsed = parse_welfare_services(
                        f, welfare_year=welfare_year, region=region,
                        org_nm=org_nm, file_dir=file_dir, uuid_nm=uuid_nm,
                        style_maps=style_maps
                    )
                    items.extend(parsed)
    except Exception as e:
        print(f"  파싱 실패 {os.path.basename(hwpx_path)}: {e}")
    return items


def process_sigun(excel_map, sigun_dir=SIGUN_DIR,
                  file_dir="/data/okms/okms/webManager/backend/WEB-INF/classes/webcont/dataFile/welfareServiceNew/2"):
    """2.시군 디렉토리의 2025/2026 HWPX 파일을 모두 파싱한다.

    폴더 구조:
        2.시군/<시군 폴더>/<연도>/*.hwpx

    각 파일에는 엑셀 메타데이터(REMARK, ITRST_TPC1/2), ORG_NM, FILE_DIR,
    UUID_NM을 붙여 반환한다. UUID_NM은 여기서 새로 만들지만,
    parse_hwpx_v6_db.py에서 기존 운영 테이블 UUID가 있으면 다시 덮어쓴다.

    sigun_dir/file_dir를 인자로 받을 수 있게 해 두어,
    parse_hwpx_v6_db.py 같은 실행 스크립트에서 문서 경로를 한 곳에서 관리할 수 있다.
    """
    region_map = {
        "창원시":"창원시","진주시":"진주시","통영시":"통영시","사천시":"사천시",
        "김해시":"김해시","밀양시":"밀양시","거제시":"거제시","양산시":"양산시",
        "의령군":"의령군","함안군":"함안군","창녕군":"창녕군","고성군":"고성군",
        "남해군":"남해군","하동군":"하동군","산청군":"산청군","함양군":"함양군",
        "거창군":"거창군","합천군":"합천군",
    }
    all_items = []
    sigun_dirs = sorted(d for d in os.listdir(sigun_dir)
                        if os.path.isdir(os.path.join(sigun_dir, d)))

    for sigun_folder in sigun_dirs:
        region = region_map.get(_folder_to_region(sigun_folder), _folder_to_region(sigun_folder))
        sigun_path = os.path.join(sigun_dir, sigun_folder)

        for year in ["2025", "2026"]:
            year_path = os.path.join(sigun_path, year)
            if not os.path.isdir(year_path):
                continue

            hwpx_files = glob.glob(os.path.join(year_path, "*.hwpx"))
            print(f"  {region} {year}: {len(hwpx_files)}개 파일")

            for hwpx_path in sorted(hwpx_files):
                file_stem = os.path.basename(hwpx_path).replace(".hwpx", "")
                meta = excel_map.get(file_stem, {})

                uuid_nm  = uuid.uuid4().hex + ".hwpx"
                org_nm   = os.path.basename(hwpx_path)

                items = _parse_hwpx(hwpx_path, welfare_year=year, region=region,
                                     org_nm=org_nm, file_dir=file_dir, uuid_nm=uuid_nm)
                for it in items:
                    it["FILE_STEM"]  = file_stem
                    it["REMARK"]     = meta.get("remark", "")
                    it["ITRST_TPC1"] = meta.get("itrst_tpc1", "")
                    it["ITRST_TPC2"] = meta.get("itrst_tpc2", "")
                all_items.extend(items)

    return all_items


def process_organ(excel_map, organ_dir=ORGAN_DIR,
                  file_dir="/data/okms/okms/webManager/backend/WEB-INF/classes/webcont/dataFile/welfareServiceNew/3"):
    """3.유관기관 디렉토리의 HWPX 파일을 모두 파싱한다.

    폴더 구조:
        3.유관기관/<기관 폴더>/*.hwpx

    기관 자료는 특정 시군/연도 폴더 구조가 아니므로 welfare_year와 region은 None으로 넘긴다.
    기관 엑셀에는 비고 컬럼이 없어 REMARK는 빈 문자열로 둔다.

    organ_dir/file_dir를 인자로 받을 수 있게 해 두어,
    parse_hwpx_v6_db.py 같은 실행 스크립트에서 문서 경로를 한 곳에서 관리할 수 있다.
    """
    all_items = []
    org_dirs = sorted(d for d in os.listdir(organ_dir)
                      if os.path.isdir(os.path.join(organ_dir, d)))

    for org_folder in org_dirs:
        org_path = os.path.join(organ_dir, org_folder)
        hwpx_files = glob.glob(os.path.join(org_path, "*.hwpx"))
        print(f"  유관기관 {org_folder}: {len(hwpx_files)}개 파일")

        for hwpx_path in sorted(hwpx_files):
            file_stem = os.path.basename(hwpx_path).replace(".hwpx", "")
            meta = excel_map.get(file_stem, {})

            uuid_nm  = uuid.uuid4().hex + ".hwpx"
            org_nm   = os.path.basename(hwpx_path)

            items = _parse_hwpx(hwpx_path, welfare_year=None, region=None,
                                 org_nm=org_nm, file_dir=file_dir, uuid_nm=uuid_nm)
            for it in items:
                it["FILE_STEM"]  = file_stem
                it["REMARK"]     = ""           # 기관 엑셀에 비고 없음
                it["ITRST_TPC1"] = meta.get("itrst_tpc1", "")
                it["ITRST_TPC2"] = meta.get("itrst_tpc2", "")
            all_items.extend(items)

    return all_items

# ──────────────────────────────────────────────────────────────
# main
#
# 이 파일을 직접 실행할 때만 동작한다.
# DB 직접 적재가 목적이면 parse_hwpx_v6_db.py를 실행하고,
# JSON/SQL 파일 산출물이 필요할 때만 이 main 경로를 사용한다.
# ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    now = datetime.now()
    ts  = now.strftime("%y%m%d%H%M")

    print("=== 엑셀 매핑 로드 ===")
    sigun_map = load_excel_sigun(EXCEL_SIGUN)
    organ_map = load_excel_organ(EXCEL_ORGAN)
    print(f"  시군 엑셀 매핑: {len(sigun_map)}건")
    print(f"  기관 엑셀 매핑: {len(organ_map)}건")

    print("\n=== 시군 파싱 ===")
    sigun_items = process_sigun(sigun_map)
    print(f"  → 총 {len(sigun_items)}건")

    print("\n=== 유관기관 파싱 ===")
    organ_items = process_organ(organ_map)
    print(f"  → 총 {len(organ_items)}건")

    all_items = sigun_items + organ_items
    print(f"\n=== 전체 합계: {len(all_items)}건 ===")

    # ── DB 행 변환 (파싱 결과 → DB 컬럼 형식) ──
    print("\n=== DB 행 변환 ===")
    db_rows = build_db_rows(all_items)
    print(f"  → DB 행 수: {len(db_rows)}건")

    # ── JSON 출력 (DB 컬럼 형식 그대로) ──
    json_path = os.path.join(SCRIPT_DIR, f"result_v4_{ts}.json")
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(db_rows, f, ensure_ascii=False, indent=2)
    print(f"JSON 저장: {json_path}")

    # ── SQL 출력 ──
    sql_path = os.path.join(SCRIPT_DIR, f"insert_v4_{ts}.sql")
    sqls = generate_sql_from_rows(db_rows)
    header = """-- =====================================================
-- parse_hwpx_v3 생성 INSERT SQL
-- 신규 컬럼 추가 필요:
--   ALTER TABLE tbl_wlf_srvc_new
--     ADD COLUMN REMARK     VARCHAR(500) NULL COMMENT '비고',
--     ADD COLUMN ITRST_TPC1 VARCHAR(100) NULL COMMENT '관심분야1',
--     ADD COLUMN ITRST_TPC2 VARCHAR(100) NULL COMMENT '관심분야2';
-- TELNO 컬럼 확장 필요 (복수 전화번호 쉼표 구분):
--   ALTER TABLE tbl_wlf_srvc_new MODIFY COLUMN TELNO VARCHAR(200) NULL;
-- =====================================================
"""
    with open(sql_path, 'w', encoding='utf-8') as f:
        f.write(header)
        f.write("\n".join(sqls))
    print(f"SQL 저장: {sql_path}")
    print(f"  INSERT 구문 수: {len(sqls)}")
