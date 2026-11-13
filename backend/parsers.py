"""打卡文件解析器：csv / tsv / xlsx / json，全部仅依赖标准库。

解析结果统一为 ParsedFile(columns, rows, warnings)，rows 为 dict 列表。
XLSX 仅支持现代 .xlsx（zip+xml），数字日期按 Excel 1900 系统还原。
"""
from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


@dataclass
class ParsedFile:
    columns: list[str]
    rows: list[dict]
    warnings: list[str] = field(default_factory=list)


def parse_text(content: bytes, fmt: str, encoding: str = "utf-8-sig", options: dict | None = None) -> ParsedFile:
    options = options or {}
    if fmt == "csv":
        delimiter = options.get("delimiter", ",")
    elif fmt == "tsv":
        delimiter = options.get("delimiter", "\t")
    elif fmt == "json":
        return _parse_json(content, encoding)
    elif fmt == "xlsx":
        return _parse_xlsx(content, options)
    else:
        raise ValueError(f"不支持的格式: {fmt}")

    text = content.decode(encoding, errors="replace")
    sample = content[:4096]
    if delimiter == "," and b"\t" in sample and b"," not in sample:
        delimiter = "\t"
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows_raw = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows_raw:
        return ParsedFile([], [])
    header_offset = int(options.get("header_row", 1)) - 1
    header = [c.strip() or f"未命名列{i+1}" for i, c in enumerate(rows_raw[header_offset])]
    data_rows = rows_raw[header_offset + 1:]
    rows: list[dict] = []
    for raw in data_rows:
        padded = raw + [""] * (len(header) - len(raw))
        rows.append({header[i]: padded[i].strip() for i in range(len(header))})
    return ParsedFile(columns=header, rows=rows)


def _parse_json(content: bytes, encoding: str) -> ParsedFile:
    data = json.loads(content.decode(encoding, errors="replace"))
    if isinstance(data, dict):
        # 兼容 {"records": [...]} 之类包裹
        for key in ("records", "data", "rows", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError("JSON 顶层必须是对象数组")
    columns: list[str] = []
    rows: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        for key in item.keys():
            if key not in columns:
                columns.append(key)
        rows.append({k: ("" if v is None else str(v)) for k, v in item.items()})
    return ParsedFile(columns=columns, rows=rows)


# ---------- XLSX ----------

def _col_index(cell_ref: str) -> int:
    letters = re.match(r"([A-Z]+)", cell_ref).group(1)
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _excel_serial(value: float) -> str:
    # Excel 1900 闰年 bug 的兼容偏移
    base = datetime(1899, 12, 30)
    dt = base + timedelta(days=value)
    if abs(value - int(value)) < 1e-9:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        xml_bytes = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(xml_bytes)
    strings = []
    for si in root.findall(f"{NS}si"):
        texts = [t.text or "" for t in si.iter(f"{NS}t")]
        strings.append("".join(texts))
    return strings


def _sheet_path(zf: zipfile.ZipFile, options: dict) -> str:
    wanted = options.get("sheet")
    names = [n for n in zf.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
    names.sort()
    if not names:
        raise ValueError("xlsx 中没有工作表")
    if not wanted:
        return names[0]
    # 支持按 sheet1/sheet2 或序号指定
    digits = "".join(ch for ch in str(wanted) if ch.isdigit())
    if digits:
        candidate = f"xl/worksheets/sheet{int(digits)}.xml"
        if candidate in names:
            return candidate
    return names[0]


def _load_date_style_ids(zf: zipfile.ZipFile) -> set[int]:
    """读取样式表，返回采用日期数字格式的 cellXfs 索引集合。"""
    date_ids: set[int] = set()
    try:
        root = ET.fromstring(zf.read("xl/styles.xml"))
    except KeyError:
        return date_ids
    numfmts: dict[int, str] = {}
    custom = root.find(f"{NS}numFmts")
    if custom is not None:
        for fmt in custom.findall(f"{NS}numFmt"):
            numfmts[int(fmt.get("numFmtId"))] = fmt.get("formatCode", "")
    # 内置日期/时间格式 ID（Excel 约定）
    builtin_date_ids = {14, 15, 16, 17, 22, 27, 30, 36, 50, 57}
    xfs = root.find(f"{NS}cellXfs")
    if xfs is None:
        return date_ids
    for idx, xf in enumerate(xfs.findall(f"{NS}xf")):
        fmt_id = int(xf.get("numFmtId", 0))
        code = numfmts.get(fmt_id, "")
        if fmt_id in builtin_date_ids or re.search(r"[ymdhs]", code.lower().replace("general", "")):
            date_ids.add(idx)
    return date_ids


def _parse_xlsx(content: bytes, options: dict) -> ParsedFile:
    warnings: list[str] = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        shared = _load_shared_strings(zf)
        date_style_ids = _load_date_style_ids(zf)
        path = _sheet_path(zf, options)
        root = ET.fromstring(zf.read(path))

    sheet_data = root.find(f"{NS}sheetData")
    grid: list[list[str]] = []
    for row_el in sheet_data.findall(f"{NS}row") if sheet_data is not None else []:
        values: dict[int, str] = {}
        max_idx = -1
        for cell in row_el.findall(f"{NS}c"):
            ref = cell.get("r", "A1")
            idx = _col_index(ref)
            max_idx = max(max_idx, idx)
            ctype = cell.get("t", "n")
            style_idx = int(cell.get("s", -1))
            v_el = cell.find(f"{NS}v")
            is_el = cell.find(f"{NS}is")
            if ctype == "s" and v_el is not None:
                value = shared[int(v_el.text)]
            elif ctype == "inlineStr" and is_el is not None:
                value = "".join(t.text or "" for t in is_el.iter(f"{NS}t"))
            elif v_el is not None:
                raw = v_el.text or ""
                if ctype == "n":
                    try:
                        num = float(raw)
                        if style_idx in date_style_ids:
                            value = _excel_serial(num)
                        else:
                            value = str(int(num)) if num.is_integer() else raw
                    except ValueError:
                        value = raw
                else:
                    value = raw
            else:
                value = ""
            values[idx] = value
        if values:
            line = [""] * (max_idx + 1)
            for idx, value in values.items():
                line[idx] = value
            grid.append(line)

    if not grid:
        return ParsedFile([], [], warnings)
    header_offset = int(options.get("header_row", 1)) - 1
    header = [c.strip() or f"未命名列{i+1}" for i, c in enumerate(grid[header_offset])]
    rows = []
    for line in grid[header_offset + 1:]:
        if not any(c.strip() for c in line):
            continue
        padded = line + [""] * (len(header) - len(line))
        rows.append({header[i]: padded[i].strip() for i in range(len(header))})
    return ParsedFile(columns=header, rows=rows, warnings=warnings)
