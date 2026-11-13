"""HTTP API 端到端测试：启动真实本地服务，验证预览->提交->汇总->导出全链路。"""
import json
import tempfile
import threading
import unittest
import urllib.request
import uuid
from pathlib import Path
from http.server import ThreadingHTTPServer

from backend import db as dbmod
from backend.server import Handler

CSV = (
    "工号,姓名,打卡时间,状态\n"
    "E001,张三,2026-11-01 09:00,上班\n"
    "E001,张三,2026-11-01 18:00,下班\n"
)


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = f"----ts{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                    .encode())
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    for name, (filename, content) in files.items():
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                    f"filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n".encode())
        body.extend(content)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        # 在任何连接建立前切换模块级数据目录，保证测试与真实 data/ 库完全隔离
        import os
        os.environ["TIMSHEET_DATA_DIR"] = cls.tmp.name
        from pathlib import Path
        import backend.db as dbmod
        import backend.server as server
        new_path = Path(cls.tmp.name) / "test_api.db"
        dbmod.DATA_DIR = Path(cls.tmp.name)
        dbmod.DB_PATH = new_path
        server.dbmod.DATA_DIR = Path(cls.tmp.name)
        server.dbmod.DB_PATH = new_path
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def _request(self, method: str, path: str, payload=None, raw_body=None, content_type=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {}
        data = None
        if raw_body is not None:
            data = raw_body
            headers["Content-Type"] = content_type
        elif payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_full_flow_over_http(self):
        status, _, body = self._request("GET", "/api/health")
        self.assertEqual(status, 200)

        # 1. 建来源
        source = {
            "code": "WEB-A", "name": "网页来源", "format": "csv",
            "time_zone": "Asia/Shanghai", "encoding": "utf-8-sig",
            "mapping": {"fields": {"employee_code": "工号", "employee_name": "姓名",
                                   "punch_time": "打卡时间", "punch_type": "状态"},
                        "punch_type_aliases": {"in": ["上班"], "out": ["下班"]}},
        }
        status, _, body = self._request("POST", "/api/sources", source)
        self.assertEqual(status, 200)
        sid = json.loads(body)["source_id"]

        # 2. 预览
        raw, ctype = _multipart({"source_id": sid}, {"file": ("p.csv", CSV.encode("utf-8"))})
        status, _, body = self._request("POST", "/api/preview", raw_body=raw, content_type=ctype)
        self.assertEqual(status, 200)
        preview = json.loads(body)
        self.assertEqual(preview["total_rows"], 2)
        self.assertIsNone(preview["duplicate_file"])

        # 3. 确认写入
        raw, ctype = _multipart({"source_id": sid, "note": "11月第一批"},
                                {"file": ("p.csv", CSV.encode("utf-8"))})
        status, _, body = self._request("POST", "/api/imports/commit", raw_body=raw, content_type=ctype)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["written"], 2)

        # 4. 重复文件应被拒
        raw, ctype = _multipart({"source_id": sid}, {"file": ("p.csv", CSV.encode("utf-8"))})
        status, _, body = self._request("POST", "/api/imports/commit", raw_body=raw, content_type=ctype)
        self.assertEqual(status, 400)
        self.assertIn("幂等", json.loads(body)["error"])

        # 5. 汇总：9 小时 = 8 正常 + 1 加班
        status, _, body = self._request("GET", "/api/summary")
        summary = json.loads(body)["summary"]
        row = next(r for r in summary if r["employee_id"] == "E001")
        self.assertEqual((row["regular_hours"], row["overtime_hours"]), (8.0, 1.0))

        # 6. 导出核对表
        status, headers, body = self._request("GET", "/api/export/reconciliation")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("正常工时", body.decode("utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
