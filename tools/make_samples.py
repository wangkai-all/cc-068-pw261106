#!/usr/bin/env python3
"""生成四种不同格式的打卡样例文件，覆盖 DST、跨夜、缺卡与冲突场景。

运行：python3 tools/make_samples.py
输出目录：samples/（仅本地使用，不会被任何外部系统读取）
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples"
OUT.mkdir(exist_ok=True)

# ---- 系统 A：中文列 CSV，纽约时区（含春季 DST 跨夜班与缺下班）----
system_a = [
    ["工号", "姓名", "打卡时间", "状态"],
    ["A1001", "张三", "2026-03-07 22:00", "上班"],
    ["A1001", "张三", "2026-03-08 06:00", "下班"],   # 春季跳表跨夜，实际 7 小时
    ["A1001", "张三", "2026-03-09 09:00", "上班"],   # 缺下班
    ["A1002", "李四", "2026-03-09 09:00", "上班"],
    ["A1002", "李四", "2026-03-09 18:00", "下班"],   # 9 小时：8 正常 + 1 加班
    ["A1002", "李四", "2026-03-10 22:30", "上班"],
    ["A1002", "李四", "2026-03-11 07:30", "下班"],   # 普通跨夜班
]
with (OUT / "system_a_2026_03.csv").open("w", encoding="utf-8-sig", newline="") as f:
    csv.writer(f).writerows(system_a)

# ---- 系统 B：英文列 TSV，伦敦时区（欧洲 2026 年回拨日为 10-25）----
system_b = [
    ["EmpID", "Clock", "Type"],
    ["B-77", "2026-10-24 23:00", "IN"],
    ["B-77", "2026-10-25 08:00", "OUT"],             # 伦敦回拨夜跨夜
    ["B-77", "2026-10-25 01:30", "IN"],              # 模糊时间（出现两次的 01:30）
    ["B-77", "2026-10-25 10:00", "OUT"],
]
with (OUT / "system_b_2026_11.tsv").open("w", encoding="utf-8", newline="") as f:
    writer = csv.writer(f, delimiter="\t")
    writer.writerows(system_b)

# ---- 系统 C：JSON 包裹格式，上海时区，日期与时刻拆列 ----
system_c = {
    "records": [
        {"emp_code": "C300", "date": "2026-03-09", "clock": "08:00", "action": "check-in"},
        {"emp_code": "C300", "date": "2026-03-09", "clock": "17:00", "action": "check-out"},
        {"emp_code": "C300", "date": "2026-03-10", "clock": "09:00", "action": "check-in"},
        # 缺下班：只有 check-in
    ]
}
(OUT / "system_c_2026_03.json").write_text(
    json.dumps(system_c, ensure_ascii=False, indent=2), encoding="utf-8")

# ---- 系统 D：XLSX（内联字符串 + Excel 日期序列号），与 A 的 A1002 在 3/9 冲突 ----
def _col_letter(idx: int) -> str:
    s, idx = "", idx + 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def _xlsx(rows: list[list[tuple[str, str]]]) -> bytes:
    """rows 单元格为 (值, 类型)，类型 s=内联字符串，d=Excel 日期序列号。"""
    sheet_rows = []
    for r_idx, row in enumerate(rows, start=1):
        cells = []
        for c_idx, (value, kind) in enumerate(row):
            ref = f"{_col_letter(c_idx)}{r_idx}"
            if kind == "d":
                cells.append(f'<c r="{ref}" s="1"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{value}</t></is></c>')
        sheet_rows.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    sheet_xml = ('<?xml version="1.0"?><worksheet '
                 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')
    styles_xml = ('<?xml version="1.0"?><styleSheet '
                  'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                  '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd h:mm"/></numFmts>'
                  '<cellXfs count="2"><xf numFmtId="0"/>'
                  '<xf numFmtId="164" applyNumberFormat="1"/></cellXfs></styleSheet>')
    content_types = ('<?xml version="1.0"?><Types '
                     'xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/worksheets/sheet1.xml" '
                     'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                     '<Override PartName="/xl/styles.xml" '
                     'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                     '</Types>')
    rels = ('<?xml version="1.0"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="x" Target="xl/worksheets/sheet1.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        zf.writestr("xl/styles.xml", styles_xml)
    return buf.getvalue()


# Excel 序列号：2026-03-09 08:30 = 46090.354166...
from datetime import datetime
base = datetime(1899, 12, 30)
def serial(text: str) -> str:
    return str((datetime.strptime(text, "%Y-%m-%d %H:%M") - base).total_seconds() / 86400)


xlsx_rows = [
    [("StaffNo", "s"), ("When", "s"), ("Mode", "s")],
    [("A1002", "s"), (serial("2026-03-09 08:30"), "d"), ("IN", "s")],
    [("A1002", "s"), (serial("2026-03-09 17:30"), "d"), ("OUT", "s")],
]
(OUT / "system_d_2026_03.xlsx").write_bytes(_xlsx(xlsx_rows))

print("已生成样例文件：")
for p in sorted(OUT.iterdir()):
    print(f"  {p.relative_to(ROOT)}  ({p.stat().st_size} 字节)")
