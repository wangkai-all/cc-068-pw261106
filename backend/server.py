"""仅依赖标准库的本地 HTTP 服务：JSON API + 前端静态文件。

启动：python3 -m backend.server  （默认 http://127.0.0.1:8765）
所有数据保存在本地 SQLite，不连接薪资系统或任何外部服务。
"""
from __future__ import annotations

import json
import mimetypes
import re
from email.parser import BytesParser
from email.policy import default as email_default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db as dbmod
from . import engine, exports

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def parse_multipart(body: bytes, content_type: str) -> dict:
    """解析 multipart/form-data，返回 {字段名: str 或 {'filename','content'}}。"""
    header = f"Content-Type: {content_type}\r\n\r\n".encode()
    msg = BytesParser(policy=email_default_policy).parsebytes(header + body)
    out: dict = {}
    if not msg.is_multipart():
        return out
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            out[name] = {"filename": filename, "content": payload}
        else:
            out[name] = payload.decode("utf-8", errors="replace")
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalTimesheet/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    # ---------- 基础收发 ----------
    def _send_json(self, payload, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_csv(self, text: str, filename: str) -> None:
        data = "﻿".encode("utf-8") + text.encode("utf-8")  # BOM 方便 Excel
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ApiError(400, f"请求体不是合法 JSON: {exc}")

    def _read_body(self) -> dict:
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if ctype.startswith("multipart/form-data"):
            return parse_multipart(body, ctype)
        if ctype.startswith("application/json"):
            try:
                return json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError as exc:
                raise ApiError(400, f"请求体不是合法 JSON: {exc}")
        raise ApiError(415, f"不支持的 Content-Type: {ctype}")

    def _db(self):
        return dbmod.session()

    # ---------- 路由 ----------
    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if path.startswith("/api/"):
                self._handle_api_get(path, query)
            else:
                self._serve_static(path)
        except ApiError as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"服务器错误: {exc}"}, 500)

    def do_POST(self) -> None:
        self._dispatch_write("POST")

    def do_PUT(self) -> None:
        self._dispatch_write("PUT")

    def _dispatch_write(self, method: str) -> None:
        try:
            path = urlparse(self.path).path
            if not path.startswith("/api/"):
                raise ApiError(404, "未找到")
            body = self._read_body()
            with self._db() as conn:
                self._handle_api_write(method, path, body, conn)
        except ApiError as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except (KeyError, ValueError) as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"服务器错误: {exc}"}, 500)

    # ---------- GET API ----------
    def _handle_api_get(self, path: str, query: dict) -> None:
        with self._db() as conn:
            if path == "/api/health":
                return self._send_json({"ok": True})
            if path == "/api/sources":
                return self._send_json({"sources": engine.list_sources(conn)})
            if path == "/api/aliases":
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM employee_alias ORDER BY source_id,employee_id").fetchall()]
                return self._send_json({"aliases": rows})
            if path == "/api/settings":
                return self._send_json({"settings": dbmod.get_settings(conn)})
            if path == "/api/batches":
                rows = [dict(r) for r in conn.execute(
                    "SELECT b.*, sc.code AS source_code, sc.name AS source_name FROM batches b "
                    "JOIN source_configs sc ON sc.id=b.source_id ORDER BY b.id DESC").fetchall()]
                return self._send_json({"batches": rows})
            if path == "/api/summary":
                return self._send_json({"summary": self._query_summary(conn, query)})
            if path == "/api/issues":
                return self._send_json({"issues": self._query_issues(conn, query)})
            if path == "/api/conflicts":
                groups = [dict(r) for r in conn.execute(
                    "SELECT * FROM conflict_groups ORDER BY status,employee_id,work_date").fetchall()]
                for g in groups:
                    g["shifts"] = [dict(r) for r in conn.execute(
                        "SELECT s.*, rin.raw_json AS in_raw, rout.raw_json AS out_raw, "
                        "sc.code AS source_code FROM shifts s "
                        "JOIN source_configs sc ON sc.id=s.source_id "
                        "LEFT JOIN punches pin ON pin.id=s.in_punch_id "
                        "LEFT JOIN raw_rows rin ON rin.id=pin.raw_row_id "
                        "LEFT JOIN punches pout ON pout.id=s.out_punch_id "
                        "LEFT JOIN raw_rows rout ON rout.id=pout.raw_row_id "
                        "WHERE s.conflict_group_id=? ORDER BY s.in_utc", (g["id"],)).fetchall()]
                    for s in g["shifts"]:
                        s["in_raw_obj"] = dbmod.loads(s.pop("in_raw"), None)
                        s["out_raw_obj"] = dbmod.loads(s.pop("out_raw"), None)
                    g["resolution"] = dbmod.loads(g.pop("resolution_json"), None)
                return self._send_json({"conflicts": groups})
            if path == "/api/trace":
                employee_id = (query.get("employee_id") or [""])[0]
                if not employee_id:
                    raise ApiError(400, "缺少 employee_id")
                return self._send_json({"trace": self._trace(conn, employee_id)})
            if path == "/api/employees":
                rows = [r["employee_id"] for r in conn.execute(
                    "SELECT DISTINCT employee_id FROM punches ORDER BY employee_id").fetchall()]
                return self._send_json({"employees": rows})
            if path == "/api/export/reconciliation":
                text = exports.export_reconciliation(
                    conn, (query.get("period_type") or [None])[0],
                    (query.get("period_key") or [None])[0],
                    (query.get("employee_id") or [None])[0])
                return self._send_csv(text, "reconciliation.csv")
            if path == "/api/export/trace":
                employee_id = (query.get("employee_id") or [""])[0]
                if not employee_id:
                    raise ApiError(400, "缺少 employee_id")
                return self._send_csv(exports.export_employee_trace(conn, employee_id),
                                      f"trace_{employee_id}.csv")
            if path == "/api/export/conflicts":
                return self._send_csv(exports.export_conflicts(conn), "conflicts.csv")
            raise ApiError(404, f"未知接口: {path}")

    # ---------- 写 API ----------
    def _handle_api_write(self, method: str, path: str, body: dict, conn) -> None:
        if path == "/api/sources" and method == "POST":
            required = ("code", "name", "format", "time_zone")
            missing = [k for k in required if not body.get(k)]
            if missing:
                raise ApiError(400, f"缺少字段: {', '.join(missing)}")
            if body["format"] not in ("csv", "tsv", "xlsx", "json"):
                raise ApiError(400, "format 必须是 csv/tsv/xlsx/json")
            sid = engine.upsert_source(
                conn, body["code"], body["name"], body["format"], body["time_zone"],
                body.get("encoding", "utf-8-sig"), body.get("options", {}), body.get("mapping", {}))
            return self._send_json({"source_id": sid})

        if path == "/api/sources/alias" and method == "POST":
            for key in ("source_id", "source_emp_code", "employee_id"):
                if body.get(key) in (None, ""):
                    raise ApiError(400, f"缺少字段: {key}")
            engine.set_alias(conn, int(body["source_id"]), str(body["source_emp_code"]),
                             str(body["employee_id"]), body.get("employee_name"))
            return self._send_json({"ok": True})

        if path == "/api/preview" and method == "POST":
            return self._file_action(conn, body, engine.preview)

        if path == "/api/imports/commit" and method == "POST":
            result = self._file_action(conn, body,
                                       lambda c, sid, content, fn: engine.commit_batch(
                                           c, sid, content, fn, body.get("note")))
            return result

        if path == "/api/recompute" and method == "POST":
            source_id = body.get("source_id")
            result = engine.recompute_all(conn, int(source_id) if source_id else None)
            return self._send_json(result)

        if path == "/api/adjustments/manual-punch" and method == "POST":
            for key in ("employee_id", "source_id", "punch_type", "local_time"):
                if not body.get(key):
                    raise ApiError(400, f"缺少字段: {key}")
            if body["punch_type"] not in ("in", "out"):
                raise ApiError(400, "punch_type 必须是 in/out")
            result = engine.add_manual_punch(
                conn, body["employee_id"], int(body["source_id"]), body["punch_type"],
                body["local_time"], body.get("reason", "手工补卡"))
            return self._send_json(result)

        if path == "/api/conflicts/resolve" and method == "POST":
            result = engine.resolve_conflict(
                conn, int(body["group_id"]), body.get("keep_shift_ids", []),
                body.get("decision", "手工选择保留班次"))
            return self._send_json(result)

        if path == "/api/batches/void" and method == "POST":
            if not body.get("batch_id"):
                raise ApiError(400, "缺少 batch_id")
            return self._send_json(engine.void_batch(conn, int(body["batch_id"])))

        if path == "/api/settings" and method == "POST":
            for key, value in body.items():
                conn.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            return self._send_json({"settings": dbmod.get_settings(conn)})

        raise ApiError(404, f"未知接口或方法不允许: {method} {path}")

    def _file_action(self, conn, body: dict, fn):
        upload = body.get("file")
        if not isinstance(upload, dict):
            raise ApiError(400, "缺少上传文件（multipart 字段名 file）")
        source_id = body.get("source_id")
        if not source_id:
            raise ApiError(400, "缺少 source_id")
        result = fn(conn, int(source_id), upload["content"], upload["filename"])
        return self._send_json(result)

    # ---------- 查询辅助 ----------
    def _query_summary(self, conn, query: dict) -> list[dict]:
        where, params = [], []
        for col in ("employee_id", "period_type", "period_key"):
            value = (query.get(col) or [None])[0]
            if value:
                where.append(f"{col}=?")
                params.append(value)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT * FROM period_summary {clause} ORDER BY employee_id,period_start", params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["regular_hours"] = round(r["regular_minutes"] / 60, 2)
            d["overtime_hours"] = round(r["overtime_minutes"] / 60, 2)
            d["total_hours"] = round((r["regular_minutes"] + r["overtime_minutes"]) / 60, 2)
            out.append(d)
        return out

    def _query_issues(self, conn, query: dict) -> list[dict]:
        where, params = [], []
        status = (query.get("status") or [None])[0]
        kind = (query.get("kind") or [None])[0]
        employee_id = (query.get("employee_id") or [None])[0]
        if status:
            where.append("i.status=?")
            params.append(status)
        if kind:
            where.append("i.kind=?")
            params.append(kind)
        if employee_id:
            where.append("i.employee_id=?")
            params.append(employee_id)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT i.*, sc.code AS source_code FROM issues i "
            f"LEFT JOIN source_configs sc ON sc.id=i.source_id {clause} ORDER BY i.id DESC", params
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["raw_refs"] = dbmod.loads(d.pop("raw_refs_json"), [])
            d["resolution"] = dbmod.loads(d.pop("resolution_json"), None)
            out.append(d)
        return out

    def _trace(self, conn, employee_id: str) -> dict:
        punches = conn.execute(
            "SELECT p.*, rr.raw_json, rr.line_no, b.code AS batch_code, sc.code AS source_code "
            "FROM punches p LEFT JOIN raw_rows rr ON rr.id=p.raw_row_id "
            "LEFT JOIN batches b ON b.id=rr.batch_id "
            "JOIN source_configs sc ON sc.id=p.source_id "
            "WHERE p.employee_id=? ORDER BY COALESCE(p.utc_ts,'9999')", (employee_id,),
        ).fetchall()
        result = []
        for p in punches:
            shift = conn.execute(
                "SELECT * FROM shifts WHERE in_punch_id=? OR out_punch_id=? LIMIT 1",
                (p["id"], p["id"])).fetchone()
            d = dict(p)
            d["raw"] = dbmod.loads(d.pop("raw_json"), None)
            d["shift"] = dict(shift) if shift else None
            result.append(d)
        return {"employee_id": employee_id, "punches": result}

    # ---------- 静态文件 ----------
    def _serve_static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        target = (FRONTEND_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(FRONTEND_DIR.resolve())) or not target.is_file():
            self.send_error(404, "Not Found")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        elif target.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="本地工时整理服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with dbmod.session() as conn:
        dbmod.init_db(conn)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"工时整理页面已启动: http://{args.host}:{args.port}  （数据目录 {dbmod.DATA_DIR}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
