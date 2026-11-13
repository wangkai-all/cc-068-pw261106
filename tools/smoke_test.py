#!/usr/bin/env python3
"""对运行中的本地服务做端到端冒烟（走真实 HTTP + multipart）。"""
import json
import sys
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8765"
SAMPLES = Path("/workspace/samples")


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def multipart(fields, file_field, filename, content):
    boundary = "----smoke" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode())
    body.extend(content)
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(BASE + "/api/preview", data=bytes(body), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def upload_commit(sid, path, note):
    boundary = "----smoke" + uuid.uuid4().hex
    content = path.read_bytes()
    body = bytearray()
    for k, v in {"source_id": sid, "note": note}.items():
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode())
    body.extend(content)
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(BASE + "/api/imports/commit", data=bytes(body), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main():
    assert call("GET", "/api/health")[1]["ok"]

    sources = [
        ("SYS-A", "打卡系统A", "csv", "America/New_York", {
            "fields": {"employee_code": "工号", "employee_name": "姓名",
                       "punch_time": "打卡时间", "punch_type": "状态"},
            "punch_type_aliases": {"in": ["上班"], "out": ["下班"]}}),
        ("SYS-B", "打卡系统B", "tsv", "Europe/London", {
            "fields": {"employee_code": "EmpID", "punch_time": "Clock", "punch_type": "Type"},
            "punch_type_aliases": {"in": ["IN"], "out": ["OUT"]}}),
        ("SYS-C", "打卡系统C", "json", "Asia/Shanghai", {
            "fields": {"employee_code": "emp_code", "punch_date": "date",
                       "punch_clock": "clock", "punch_type": "action"},
            "punch_type_aliases": {"in": ["check-in"], "out": ["check-out"]}}),
        ("SYS-D", "打卡系统D", "xlsx", "America/New_York", {
            "fields": {"employee_code": "StaffNo", "punch_time": "When", "punch_type": "Mode"},
            "punch_type_aliases": {"in": ["IN"], "out": ["OUT"]}}),
    ]
    ids = {}
    for code, name, fmt, tz, mapping in sources:
        status, data = call("POST", "/api/sources",
                            {"code": code, "name": name, "format": fmt, "time_zone": tz,
                             "options": {"header_row": 1}, "mapping": mapping})
        assert status == 200, data
        ids[code] = data["source_id"]

    # 把 D 的 A1002 与 A 的 A1002 视为同一员工（本来编号就相同，直接制造同日冲突）
    # 预览各文件
    pv_a = multipart({"source_id": ids["SYS-A"]}, "file",
                     "system_a_2026_03.csv", (SAMPLES / "system_a_2026_03.csv").read_bytes())
    assert pv_a["total_rows"] == 7, pv_a
    assert pv_a["error_count"] == 0
    print("预览 A：", pv_a["total_rows"], "行，DST 标记行数 =",
          sum(1 for r in pv_a["rows"] if r["dst_kind"] != "ok"))

    pv_d = multipart({"source_id": ids["SYS-D"]}, "file",
                     "system_d_2026_03.xlsx", (SAMPLES / "system_d_2026_03.xlsx").read_bytes())
    assert pv_d["total_rows"] == 2, pv_d
    assert pv_d["rows"][0]["local_time"].startswith("2026-03-09T08:30"), pv_d["rows"][0]
    print("预览 D（xlsx 日期序列号解析）：", pv_d["rows"][0]["local_time"])

    # 提交四个来源
    r = upload_commit(ids["SYS-A"], SAMPLES / "system_a_2026_03.csv", "3月A")
    assert r[0] == 200, r
    print("提交 A：", r[1])
    # 重复提交必须被拒
    r2 = upload_commit(ids["SYS-A"], SAMPLES / "system_a_2026_03.csv", "重复")
    assert r2[0] == 400 and "幂等" in r2[1]["error"], r2
    print("重复文件被拒：", r2[1]["error"])

    for code, fname in [("SYS-B", "system_b_2026_11.tsv"),
                        ("SYS-C", "system_c_2026_03.json"),
                        ("SYS-D", "system_d_2026_03.xlsx")]:
        st, data = upload_commit(ids[code], SAMPLES / fname, code)
        assert st == 200, (code, data)
        print(f"提交 {code}：", data)

    # 验证汇总关键数字
    _, summ = call("GET", "/api/summary")
    rows = {(r["employee_id"], r["period_key"]): r for r in summ["summary"]}
    a1001 = [r for r in summ["summary"] if r["employee_id"] == "A1001"]
    print("A1001 周期：", [(r["period_key"], r["regular_hours"], r["incomplete_shifts"]) for r in a1001])
    # 3/7 跨夜 DST 班 7h（420 分钟），3/9 缺卡不计
    mar = next(r for r in a1001 if r["period_key"] == "2026-03")
    assert mar["regular_minutes"] == 420, mar
    assert mar["incomplete_shifts"] == 1, mar

    # 冲突：A1002 在 2026-03-09 有 A 与 D 两套
    _, conflicts = call("GET", "/api/conflicts")
    open_groups = [g for g in conflicts["conflicts"] if g["status"] == "open"]
    assert any(g["employee_id"] == "A1002" and g["work_date"] == "2026-03-09" for g in open_groups), conflicts
    g = next(g for g in open_groups if g["employee_id"] == "A1002")
    print("冲突组：", g["employee_id"], g["work_date"], "候选班次数 =", len(g["shifts"]))
    keep = next(s["id"] for s in g["shifts"] if s["source_code"] == "SYS-A")
    st, data = call("POST", "/api/conflicts/resolve",
                    {"group_id": g["id"], "keep_shift_ids": [keep], "decision": "冒烟测试保留A"})
    assert st == 200, data

    # 给 A1001 的缺下班补卡
    st, data = call("POST", "/api/adjustments/manual-punch", {
        "employee_id": "A1001", "source_id": ids["SYS-A"], "punch_type": "out",
        "local_time": "2026-03-09 18:00", "reason": "冒烟补卡"})
    assert st == 200, data
    _, summ2 = call("GET", "/api/summary")
    mar2 = next(r for r in summ2["summary"] if r["employee_id"] == "A1001" and r["period_key"] == "2026-03")
    # 7h 跨夜班 + 9h 补卡班(8正常+1加班) => 正常 420+480=900，加班 60
    assert (mar2["regular_minutes"], mar2["overtime_minutes"]) == (900, 60), mar2
    print("补卡后 A1001：正常", mar2["regular_hours"], "h 加班", mar2["overtime_hours"], "h")

    # 导出
    with urllib.request.urlopen(BASE + "/api/export/reconciliation") as r:
        text = r.read().decode("utf-8-sig")
        assert "正常工时(小时)" in text and "A1001" in text
    with urllib.request.urlopen(BASE + "/api/export/trace?employee_id=A1001") as r:
        trace = r.read().decode("utf-8-sig")
        assert "手工补卡" in trace and "归属工作日" in trace
    print("导出核对表与员工追溯 CSV：OK")

    # 全量重算幂等性：结果应保持稳定
    st, rr = call("POST", "/api/recompute", {})
    assert st == 200, rr
    _, summ3 = call("GET", "/api/summary")
    mar3 = next(r for r in summ3["summary"] if r["employee_id"] == "A1001" and r["period_key"] == "2026-03")
    assert (mar3["regular_minutes"], mar3["overtime_minutes"]) == (900, 60), mar3
    print("全量重算后结果稳定：OK；build =", rr)
    print("\n全部冒烟断言通过 ✅")


if __name__ == "__main__":
    main()
