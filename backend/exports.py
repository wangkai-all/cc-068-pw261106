"""核对表与原始行追溯的 CSV 导出（纯本地生成，不向外部系统发送任何数据）。"""
from __future__ import annotations

import csv
import io

from . import db as dbmod


def _csv_response(headers: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue()


def export_reconciliation(conn, period_type: str | None = None, period_key: str | None = None,
                          employee_id: str | None = None) -> str:
    where, params = [], []
    if period_type:
        where.append("period_type=?")
        params.append(period_type)
    if period_key:
        where.append("period_key=?")
        params.append(period_key)
    if employee_id:
        where.append("employee_id=?")
        params.append(employee_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    rows = conn.execute(
        f"SELECT * FROM period_summary {clause} ORDER BY employee_id,period_start", params,
    ).fetchall()
    open_missing = {}
    for r in conn.execute(
        "SELECT p.employee_id, COUNT(*) AS c FROM issues i "
        "JOIN punches p ON p.id=i.punch_id WHERE i.status='open' AND i.kind='missing_punch' "
        "GROUP BY p.employee_id"
    ).fetchall():
        open_missing[r["employee_id"]] = r["c"]

    data = []
    for r in rows:
        data.append([
            r["employee_id"], r["period_type"], r["period_key"],
            r["period_start"], r["period_end"],
            round(r["regular_minutes"] / 60, 2),
            round(r["overtime_minutes"] / 60, 2),
            round((r["regular_minutes"] + r["overtime_minutes"]) / 60, 2),
            r["overnight_shifts"], r["dst_crossing_shifts"],
            r["complete_shifts"], r["incomplete_shifts"],
            r["conflict_count"], open_missing.get(r["employee_id"], 0),
        ])
    return _csv_response(
        ["员工编号", "周期类型", "周期", "开始日期", "结束日期",
         "正常工时(小时)", "加班(小时)", "合计(小时)",
         "跨夜班次", "DST跨切换班次", "完整班次", "缺卡班次", "冲突组数", "待补卡数"],
        data,
    )


def export_employee_trace(conn, employee_id: str) -> str:
    """按员工追溯：每条归一化打卡对应到原始行、批次与计算出的班次。"""
    punches = conn.execute(
        "SELECT p.*, rr.raw_json, rr.line_no, b.code AS batch_code, sc.code AS source_code "
        "FROM punches p LEFT JOIN raw_rows rr ON rr.id=p.raw_row_id "
        "LEFT JOIN batches b ON b.id=rr.batch_id "
        "JOIN source_configs sc ON sc.id=p.source_id "
        "WHERE p.employee_id=? ORDER BY COALESCE(p.utc_ts,'9999')", (employee_id,),
    ).fetchall()
    data = []
    for p in punches:
        shift = conn.execute(
            "SELECT work_date,total_minutes,overnight,dst_crossing,complete FROM shifts "
            "WHERE in_punch_id=? OR out_punch_id=? LIMIT 1", (p["id"], p["id"]),
        ).fetchone()
        data.append([
            p["source_code"], p["batch_code"] or ("手工补卡" if p["manual"] else ""),
            p["line_no"] or "", p["punch_type"], p["local_ts"] or "(缺卡)",
            p["utc_ts"] or "", "是" if p["manual"] else "否",
            shift["work_date"] if shift else "",
            (round(shift["total_minutes"] / 60, 2) if shift and shift["total_minutes"] is not None
             else ("未计时" if shift else "")),
            ("是" if shift and shift["overnight"] else ""),
            ("是" if shift and shift["dst_crossing"] else ""),
            p["raw_json"] or "", p["note"] or "",
        ])
    return _csv_response(
        ["来源", "批次", "原始行号", "打卡类型", "本地时间", "UTC时间", "手工",
         "归属工作日", "班次工时(小时)", "跨夜", "DST", "原始行内容", "备注"],
        data,
    )


def export_conflicts(conn) -> str:
    groups = conn.execute("SELECT * FROM conflict_groups ORDER BY status, employee_id, work_date").fetchall()
    data = []
    for g in groups:
        issue = conn.execute(
            "SELECT raw_refs_json FROM issues WHERE conflict_group_id=? AND kind='conflict'", (g["id"],),
        ).fetchone()
        refs = dbmod.loads(issue["raw_refs_json"], []) if issue else []
        data.append([
            g["employee_id"], g["work_date"], g["status"],
            "; ".join(f"{r.get('batch_code','')}#L{r.get('line_no','')}" for r in refs),
        ])
    return _csv_response(["员工编号", "工作日", "状态", "涉及原始行"], data)
