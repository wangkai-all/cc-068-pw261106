"""csv/tsv/json/xlsx 解析器测试。"""
import io
import unittest
import zipfile

from backend import parsers


def build_minimal_xlsx(rows: list[list[str]], date_cells: dict[tuple[int, int], float] | None = None) -> bytes:
    """构造最小可解析 xlsx：内联字符串 + 可选数字日期单元格。"""
    date_cells = date_cells or {}

    def col_letter(idx: int) -> str:
        s = ""
        idx += 1
        while idx:
            idx, rem = divmod(idx - 1, 26)
            s = chr(65 + rem) + s
        return s

    sheet_rows = []
    for r_idx, row in enumerate(rows, start=1):
        cells = []
        for c_idx, value in enumerate(row):
            ref = f"{col_letter(c_idx)}{r_idx}"
            if (r_idx - 1, c_idx) in date_cells:
                num = date_cells[(r_idx - 1, c_idx)]
                cells.append(f'<c r="{ref}" s="1"><v>{num}</v></c>')
            else:
                cells.append(
                    f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{value}</t></is></c>')
        sheet_rows.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    sheet_xml = (
        '<?xml version="1.0"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    styles_xml = (
        '<?xml version="1.0"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd h:mm"/></numFmts>'
        '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164" applyNumberFormat="1"/></cellXfs>'
        '</styleSheet>'
    )
    content_types = (
        '<?xml version="1.0"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="x" Target="xl/worksheets/sheet1.xml"/></Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        zf.writestr("xl/styles.xml", styles_xml)
    return buf.getvalue()


class ParserTest(unittest.TestCase):
    def test_csv(self):
        parsed = parsers.parse_text("a,b\n1,2\n3,4\n".encode(), "csv")
        self.assertEqual(parsed.columns, ["a", "b"])
        self.assertEqual(parsed.rows, [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}])

    def test_tsv(self):
        parsed = parsers.parse_text("a\tb\n1\t2\n".encode(), "tsv")
        self.assertEqual(parsed.rows, [{"a": "1", "b": "2"}])

    def test_json_wrapped(self):
        payload = '{"records":[{"工号":"E1","时间":"2026-01-01 09:00"}]}'.encode("utf-8")
        parsed = parsers.parse_text(payload, "json")
        self.assertIn("工号", parsed.columns)
        self.assertEqual(parsed.rows[0]["工号"], "E1")

    def test_xlsx_inline_strings_and_date_serial(self):
        # Excel 序列 46023.375 = 2026-01-01 09:00
        xlsx = build_minimal_xlsx(
            [["工号", "打卡时间"], ["E1", "46023.375"]],
            date_cells={(1, 1): 46023.375},
        )
        parsed = parsers.parse_text(xlsx, "xlsx")
        self.assertEqual(parsed.columns, ["工号", "打卡时间"])
        self.assertEqual(parsed.rows[0]["工号"], "E1")
        self.assertTrue(parsed.rows[0]["打卡时间"].startswith("2026-01-01 09:00"))


if __name__ == "__main__":
    unittest.main()
