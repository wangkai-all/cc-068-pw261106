"""工时计算引擎：导入预览/提交、班次构建、问题检测、周期汇总与重算。

关键规则集中在本模块，配合 timecalc.py：
- 文件级幂等：同来源相同内容哈希拒绝重复提交；
- 行级幂等：同内容行哈希已存在则跳过，绝不重复计时；
- 缺卡：班次 total_minutes 为 NULL，不计工时，只登记 missing_punch，等待补卡；
- 冲突：同员工同一工作日出现多套互斥记录时成组挂起，解决前不计入汇总；
- 重算：修订映射后按受影响来源/员工重建 punches（手工补卡保留）、shifts、issues、汇总。
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from . import db as dbmod
from . import mapping as mappingmod
from . import parsers, timecalc


# ---------- 配置读取 ----------

def get_source(conn, source_id: int) -> dict:
    row = conn.execute("SELECT * FROM source_configs WHERE id=?", (source_id,)).fetchone()
    if row is None:
        raise KeyError(f"来源不存在: {source_id}")
    d = dict(row)
    d["options"] = dbmod.loads(d.pop("options_json"), {})
    d["mapping"] = dbmod.loads(d.pop("mapping_json"), {})
    return d


def list_sources(conn) -> list[dict]:
    out = []
    for row in conn.execute("SELECT * FROM source_configs ORDER BY id").fetchall():
        d = dict(row)
        d["options"] = dbmod.loads(d.pop("options_json"), {})
        d["mapping"] = dbmod.loads(d.pop("mapping_json"), {})
        out.append(d)
    return out


def upsert_source(conn, code: str, name: str, fmt: str, time_zone: str,
                  encoding: str, options: dict, mapping: dict) -> int:
    existing = conn.execute("SELECT id FROM source_configs WHERE code=?", (code,)).fetchone()
    if existing:
        source_id = existing["id"]
        conn.execute(
            "UPDATE source_configs SET name=?,format=?,time_zone=?,encoding=?,"
            "options_json=?,mapping_json=?,updated_at=datetime('now') WHERE id=?",
            (name, fmt, time_zone, encoding, dbmod.dumps(options), dbmod.dumps(mapping), source_id),
        )
        _record_mapping_version(conn, source_id, mapping)
        return source_id
    cur = conn.execute(
        "INSERT INTO source_configs(code,name,format,time_zone,encoding,options_json,mapping_json)"
        " VALUES(?,?,?,?,?,?,?)",
        (code, name, fmt, time_zone, encoding, dbmod.dumps(options), dbmod.dumps(mapping)),
    )
    source_id = cur.lastrowid
    _record_mapping_version(conn, source_id, mapping)
    return source_id


def _record_mapping_version(conn, source_id: int, mapping: dict) -> None:
    row = conn.execute("SELECT COALESCE(MAX(version),0)+1 AS v FROM mapping_versions WHERE source_id=?",
                       (source_id,)).fetchone()
    conn.execute(
        "INSERT INTO mapping_versions(source_id,version,mapping_json) VALUES(?,?,?)",
        (source_id, row["v"], dbmod.dumps(mapping)),
    )


def set_alias(conn, source_id: int, source_emp_code: str, employee_id: str, employee_name: str | None) -> None:
    conn.execute(
        "INSERT INTO employee_alias(source_id,source_emp_code,employee_id,employee_name) VALUES(?,?,?,?) "
        "ON CONFLICT(source_id,source_emp_code) DO UPDATE SET employee_id=excluded.employee_id,"
        "employee_name=excluded.employee_name",
        (source_id, source_emp_code, employee_id, employee_name),
    )


# ---------- 预览（不落库） ----------

def preview(conn, source_id: int, content: bytes, file_name: str) -> dict:
    source = get_source(conn, source_id)
    content_hash = hashlib.sha256(content).hexdigest()
    dup_file = conn.execute(
        "SELECT id,file_name FROM import_files WHERE source_id=? AND content_hash=?",
        (source_id, content_hash),
    ).fetchone()

    parsed = parsers.parse_text(content, source["format"], source["encoding"], source["options"])
    existing_hashes = {
        r["row_hash"]
        for r in conn.execute(
            "SELECT rr.row_hash FROM raw_rows rr JOIN batches b ON b.id=rr.batch_id "
            "WHERE rr.source_id=?", (source_id,)
        ).fetchall()
    }
    aliases = {
        r["source_emp_code"]: dict(r)
        for r in conn.execute("SELECT * FROM employee_alias WHERE source_id=?", (source_id,)).fetchall()
    }

    rows = []
    error_count = 0
    new_emp_codes: dict[str, str | None] = {}
    duplicate_rows = 0
    for idx, raw in enumerate(parsed.rows, start=1):
        line_no = idx + int(source["options"].get("header_row", 1))
        p = mappingmod.normalize_row(raw, line_no, source["mapping"], source["time_zone"],
                                     conn.execute("SELECT value FROM settings WHERE key='fold_policy'")
                                     .fetchone()["value"])
        if p.row_hash in existing_hashes:
            duplicate_rows += 1
        alias = aliases.get(p.source_emp_code) if p.source_emp_code else None
        if p.source_emp_code and alias is None:
            new_emp_codes.setdefault(p.source_emp_code, p.employee_name)
        if p.errors:
            error_count += 1
        rows.append({
            "line_no": p.line_no,
            "source_emp_code": p.source_emp_code,
            "mapped_employee_id": alias["employee_id"] if alias else None,
            "employee_name": (alias["employee_name"] if alias else None) or p.employee_name,
            "punch_type": p.punch_type,
            "local_time": p.local_naive_iso,
            "dst_kind": p.dst_kind,
            "row_hash": p.row_hash[:12],
            "duplicate": p.row_hash in existing_hashes,
            "errors": p.errors,
            "warnings": p.warnings,
            "raw": p.raw,
        })

    return {
        "source": {"id": source_id, "code": source["code"], "name": source["name"],
                   "format": source["format"], "time_zone": source["time_zone"]},
        "file_name": file_name,
        "content_hash": content_hash,
        "columns": parsed.columns,
        "total_rows": len(rows),
        "error_count": error_count,
        "duplicate_file": dict(dup_file) if dup_file else None,
        "duplicate_rows": duplicate_rows,
        "new_employee_codes": [{"source_emp_code": k, "employee_name": v} for k, v in new_emp_codes.items()],
        "rows": rows,
        "warnings": parsed.warnings,
    }


# ---------- 确认写入批次 ----------

def commit_batch(conn, source_id: int, content: bytes, file_name: str, note: str | None = None) -> dict:
    source = get_source(conn, source_id)
    content_hash = hashlib.sha256(content).hexdigest()
    dup = conn.execute(
        "SELECT id FROM import_files WHERE source_id=? AND content_hash=?",
        (source_id, content_hash),
    ).fetchone()
    if dup:
        raise ValueError("同一来源的相同文件已导入，禁止重复计时（文件级幂等）")

    parsed = parsers.parse_text(content, source["format"], source["encoding"], source["options"])
    cur = conn.execute(
        "INSERT INTO import_files(source_id,file_name,content_hash,byte_size) VALUES(?,?,?,?)",
        (source_id, file_name, content_hash, len(content)),
    )
    file_id = cur.lastrowid

    code = f"B{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{source['code']}"
    cur = conn.execute(
        "INSERT INTO batches(source_id,file_id,code,note) VALUES(?,?,?,?)",
        (source_id, file_id, code, note),
    )
    batch_id = cur.lastrowid

    existing = {
        r["row_hash"] for r in conn.execute(
            "SELECT row_hash FROM raw_rows WHERE source_id=?", (source_id,)
        ).fetchall()
    }
    fold_policy = conn.execute("SELECT value FROM settings WHERE key='fold_policy'").fetchone()["value"]
    written, skipped, failed = 0, 0, 0
    for idx, raw in enumerate(parsed.rows, start=1):
        line_no = idx + int(source["options"].get("header_row", 1))
        p = mappingmod.normalize_row(raw, line_no, source["mapping"], source["time_zone"], fold_policy)
        if p.row_hash in existing:
            skipped += 1
            continue
        existing.add(p.row_hash)
        cur = conn.execute(
            "INSERT INTO raw_rows(batch_id,source_id,file_id,line_no,row_hash,raw_json) VALUES(?,?,?,?,?,?)",
            (batch_id, source_id, file_id, line_no, p.row_hash, dbmod.dumps(p.raw)),
        )
        raw_row_id = cur.lastrowid
        if p.errors:
            failed += 1
            conn.execute(
                "INSERT INTO issues(kind,severity,employee_id,source_id,work_date,punch_id,message,raw_refs_json)"
                " VALUES('unpaired_punch','error',NULL,?,NULL,NULL,?,?)",
                (source_id, f"第{line_no}行解析失败: {'; '.join(p.errors)}",
                 dbmod.dumps([{"batch_id": batch_id, "raw_row_id": raw_row_id, "line_no": line_no}])),
            )
            continue
        alias = conn.execute(
            "SELECT * FROM employee_alias WHERE source_id=? AND source_emp_code=?",
            (source_id, p.source_emp_code),
        ).fetchone()
        employee_id = alias["employee_id"] if alias else p.source_emp_code
        _insert_punch(conn, source_id, batch_id, raw_row_id, employee_id, p.source_emp_code,
                      p.punch_type or "in", p.local_naive_iso, source["time_zone"], fold_policy,
                      "; ".join(p.warnings) or None)
        written += 1

    conn.execute("UPDATE batches SET row_count=? WHERE id=?", (written, batch_id))
    recompute_all(conn)
    return {"batch_id": batch_id, "code": code, "written": written, "skipped_duplicates": skipped,
            "failed_rows": failed, "file_id": file_id}


# ---------- 班次构建与重算 ----------

def _clear_derived(conn, employee_ids: tuple[str, ...] | None = None,
                   source_id: int | None = None) -> None:
    """重算前清理派生数据；手工补卡（manual=1）保留。

    按来源重算（source_id 非空）时，只删除该来源的打卡，受影响员工的其他来源打卡必须保留；
    班次/冲突/汇总会在随后基于该员工的全部打卡重建。
    """
    if employee_ids:
        placeholders = ",".join("?" for _ in employee_ids)
        conn.execute(f"DELETE FROM period_summary WHERE employee_id IN ({placeholders})", employee_ids)
        if source_id is not None:
            # 仅清理当前来源的问题；冲突问题跨来源，按员工清理后由 _build_conflicts 重建
            conn.execute(f"DELETE FROM issues WHERE employee_id IN ({placeholders}) "
                         "AND (source_id=? OR kind='conflict')",
                         (*employee_ids, source_id))
            conn.execute(f"DELETE FROM punches WHERE employee_id IN ({placeholders}) "
                         "AND source_id=? AND manual=0", (*employee_ids, source_id))
        else:
            conn.execute(f"DELETE FROM issues WHERE employee_id IN ({placeholders}) "
                         "AND kind IN ('missing_punch','unpaired_punch','long_shift','conflict',"
                         "'nonexistent_time','ambiguous_time')",
                         employee_ids)
            conn.execute(f"DELETE FROM punches WHERE employee_id IN ({placeholders}) AND manual=0",
                         employee_ids)
        conn.execute(f"DELETE FROM shifts WHERE employee_id IN ({placeholders})", employee_ids)
        conn.execute(f"DELETE FROM conflict_groups WHERE employee_id IN ({placeholders})", employee_ids)
    else:
        conn.execute("DELETE FROM period_summary")
        # 解析失败类问题（无 punch_id 的 unpaired_punch）保留，由 _regenerate_punches 去重
        conn.execute("DELETE FROM issues WHERE punch_id IS NOT NULL OR kind IN "
                     "('missing_punch','long_shift','conflict',"
                     "'nonexistent_time','ambiguous_time')")
        conn.execute("DELETE FROM shifts")
        conn.execute("DELETE FROM conflict_groups")
        conn.execute("DELETE FROM punches WHERE manual=0")


def _regenerate_punches(conn, source_id: int | None = None) -> None:
    """从 raw_rows 按当前映射重新生成非手工打卡。"""
    where = "WHERE rr.source_id=?" if source_id is not None else ""
    params = (source_id,) if source_id is not None else ()
    rows = conn.execute(
        f"SELECT rr.*, sc.time_zone, sc.mapping_json, sc.encoding, sc.options_json, b.id AS b_id "
        f"FROM raw_rows rr JOIN source_configs sc ON sc.id=rr.source_id "
        f"JOIN batches b ON b.id=rr.batch_id {where} ORDER BY rr.id", params,
    ).fetchall()
    fold_policy = conn.execute("SELECT value FROM settings WHERE key='fold_policy'").fetchone()["value"]
    for r in rows:
        mapping = dbmod.loads(r["mapping_json"], {})
        options = dbmod.loads(r["options_json"], {})
        raw = dbmod.loads(r["raw_json"], {})
        p = mappingmod.normalize_row(raw, r["line_no"], mapping, r["time_zone"], fold_policy)
        if p.errors:
            conn.execute(
                "INSERT INTO issues(kind,severity,source_id,work_date,message,raw_refs_json)"
                " VALUES('unpaired_punch','error',?,NULL,?,?)",
                (r["source_id"], f"第{r['line_no']}行解析失败: {'; '.join(p.errors)}",
                 dbmod.dumps([{"batch_id": r["b_id"], "raw_row_id": r["id"], "line_no": r["line_no"]}])),
            )
            continue
        alias = conn.execute(
            "SELECT * FROM employee_alias WHERE source_id=? AND source_emp_code=?",
            (r["source_id"], p.source_emp_code),
        ).fetchone()
        employee_id = alias["employee_id"] if alias else p.source_emp_code
        _insert_punch(conn, r["source_id"], r["b_id"], r["id"], employee_id, p.source_emp_code,
                      p.punch_type or "in", p.local_naive_iso, r["time_zone"], fold_policy,
                      "; ".join(p.warnings) or None)


def _add_issue(conn, kind, severity, employee_id, source_id, work_date, punch_id,
               message, raw_refs, conflict_group_id=None) -> int:
    cur = conn.execute(
        "INSERT INTO issues(kind,severity,employee_id,source_id,work_date,punch_id,conflict_group_id,"
        "message,raw_refs_json) VALUES(?,?,?,?,?,?,?,?,?)",
        (kind, severity, employee_id, source_id, work_date, punch_id, conflict_group_id,
         message, dbmod.dumps(raw_refs)),
    )
    return cur.lastrowid


def _dst_issue_kind(dst_kind: str) -> tuple[str, str, str] | None:
    """返回 (issue kind, severity, 说明)，非 DST 特殊时间返回 None。"""
    if dst_kind == "nonexistent":
        return ("nonexistent_time", "warning",
                "打卡时间落在夏令时春季跳变缺口（本地时间不存在），已按跳变前偏移换算，请核查")
    if dst_kind == "ambiguous":
        return ("ambiguous_time", "warning",
                "打卡时间为夏令时秋季回拨的模糊时间，已按 fold 规则解释，请核查")
    return None


def _insert_punch(conn, source_id, batch_id, raw_row_id, employee_id, source_emp_code,
                  punch_type, local_naive_iso, tz_name, fold_policy, note) -> int:
    """插入归一化打卡，并在 DST 缺口/模糊时间上登记独立问题。"""
    utc_iso = None
    dst_kind = "ok"
    if local_naive_iso:
        resolved = timecalc.resolve_local(
            datetime.fromisoformat(local_naive_iso), tz_name, fold_policy)
        utc_iso = resolved.utc.isoformat() if resolved.utc else None
        dst_kind = resolved.kind
    cur = conn.execute(
        "INSERT INTO punches(source_id,batch_id,raw_row_id,employee_id,source_emp_code,punch_type,"
        "local_ts,tz_name,utc_ts,fold_policy,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (source_id, batch_id, raw_row_id, employee_id, source_emp_code,
         punch_type, local_naive_iso, tz_name, utc_iso, fold_policy, note),
    )
    punch_id = cur.lastrowid
    special = _dst_issue_kind(dst_kind)
    if special:
        kind, severity, message = special
        _add_issue(conn, kind, severity, employee_id, source_id,
                   local_naive_iso[:10] if local_naive_iso else None,
                   punch_id, message, [_raw_ref(conn, punch_id)])
    return punch_id


def _raw_ref(conn, punch_id) -> dict | None:
    r = conn.execute(
        "SELECT rr.id AS raw_row_id, rr.batch_id, rr.line_no, b.code AS batch_code "
        "FROM punches p LEFT JOIN raw_rows rr ON rr.id=p.raw_row_id "
        "LEFT JOIN batches b ON b.id=rr.batch_id WHERE p.id=?", (punch_id,),
    ).fetchone()
    return dict(r) if r else None


def recompute_all(conn, source_id: int | None = None) -> dict:
    settings = dbmod.get_settings(conn)
    long_shift = int(settings["long_shift_minutes"])
    daily_regular = int(settings["daily_regular_minutes"])

    affected = _affected_employees(conn, source_id)
    _clear_derived(conn, affected if source_id is not None else None, source_id)
    _regenerate_punches(conn, source_id)

    punch_rows = conn.execute(
        "SELECT * FROM punches ORDER BY employee_id, source_id, COALESCE(utc_ts, '9999'), id"
    ).fetchall()
    # 关键：配对必须按 (员工, 来源) 各自独立，避免跨来源交错配成错误班次
    by_employee_source: dict[tuple, list] = {}
    for r in punch_rows:
        by_employee_source.setdefault((r["employee_id"], r["source_id"]), []).append(r)

    shifts_built = 0
    affected_set = set(affected)
    for (employee_id, _src), punches in by_employee_source.items():
        if source_id is not None and employee_id not in affected_set:
            continue
        shifts_built += _build_employee_shifts(conn, employee_id, punches, long_shift, daily_regular)

    _build_conflicts(conn)
    _rebuild_summary(conn, settings)
    open_issues = conn.execute("SELECT COUNT(*) AS c FROM issues WHERE status='open'").fetchone()["c"]
    cur = conn.execute(
        "INSERT INTO build_runs(scope_json,shifts_built,issues_open) VALUES(?,?,?)",
        (dbmod.dumps({"source_id": source_id, "employees": affected}), shifts_built, open_issues),
    )
    return {"build_run_id": cur.lastrowid, "shifts_built": shifts_built, "open_issues": open_issues,
            "affected_employees": affected}


def _affected_employees(conn, source_id: int | None) -> tuple[str, ...]:
    if source_id is None:
        return tuple(r["employee_id"] for r in conn.execute(
            "SELECT DISTINCT employee_id FROM punches").fetchall())
    return tuple(r["employee_id"] for r in conn.execute(
        "SELECT DISTINCT employee_id FROM punches WHERE source_id=?", (source_id,)).fetchall())


def _build_employee_shifts(conn, employee_id: str, punches: list, long_shift: int,
                           daily_regular: int) -> int:
    open_shift: dict | None = None
    built = 0

    def flush(shift: dict) -> None:
        nonlocal built
        _persist_shift(conn, employee_id, shift, long_shift, daily_regular)
        built += 1

    for punch in punches:
        # 缺卡行：不参与配对，直接登记问题，绝不当零工时
        if punch["utc_ts"] is None:
            _add_issue(conn, "missing_punch", "error", employee_id, punch["source_id"],
                       None, punch["id"],
                       f"第{punch['id']}条打卡缺少{punch['punch_type']}时间，未计入工时，等待补卡",
                       [_raw_ref(conn, punch["id"])])
            continue

        ptype = punch["punch_type"]
        if ptype not in ("in", "out"):
            ptype = "in" if open_shift is None else "out"

        if ptype == "in":
            if open_shift is not None:
                # 连续上班卡：上一班缺下班
                _add_issue(conn, "missing_punch", "error", employee_id, open_shift["in"]["source_id"],
                           None, open_shift["in"]["id"],
                           "连续出现上班卡，上一班缺少下班时间，未计入工时",
                           [_raw_ref(conn, open_shift["in"]["id"])])
                open_shift["complete"] = False
                flush(open_shift)
            open_shift = {"in": punch, "out": None, "complete": False}
        else:  # out
            if open_shift is None:
                _add_issue(conn, "unpaired_punch", "warning", employee_id, punch["source_id"],
                           None, punch["id"], "下班卡缺少配对的上班卡",
                           [_raw_ref(conn, punch["id"])])
                continue
            open_shift["out"] = punch
            open_shift["complete"] = True
            flush(open_shift)
            open_shift = None

    if open_shift is not None:
        _add_issue(conn, "missing_punch", "error", employee_id, open_shift["in"]["source_id"],
                   None, open_shift["in"]["id"], "班次缺少下班时间，未计入工时，等待补卡",
                   [_raw_ref(conn, open_shift["in"]["id"])])
        flush(open_shift)
    return built


def _persist_shift(conn, employee_id: str, shift: dict, long_shift: int, daily_regular: int) -> None:
    in_p, out_p = shift["in"], shift["out"]
    in_utc = datetime.fromisoformat(in_p["utc_ts"]) if in_p["utc_ts"] else None
    out_utc = datetime.fromisoformat(out_p["utc_ts"]) if out_p and out_p["utc_ts"] else None
    in_local = timecalc.utc_to_local_iso(in_utc, in_p["tz_name"])
    work_date = in_local[:10]
    out_local = timecalc.utc_to_local_iso(out_utc, out_p["tz_name"]) if out_utc else None
    overnight = 0
    dst = 0
    total = regular = overtime = None
    if shift["complete"] and in_utc and out_utc:
        overnight = 1 if out_local[:10] != work_date else 0
        dst = 1 if timecalc.is_dst_crossing(in_utc, out_utc, in_p["tz_name"]) else 0
        total = timecalc.elapsed_minutes(in_utc, out_utc)
        regular, overtime = timecalc.split_overtime(total, daily_regular)
    conn.execute(
        "INSERT INTO shifts(employee_id,source_id,work_date,in_punch_id,out_punch_id,in_utc,out_utc,"
        "in_local,out_local,tz_name,overnight,dst_crossing,total_minutes,regular_minutes,"
        "overtime_minutes,complete) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (employee_id, in_p["source_id"], work_date, in_p["id"],
         out_p["id"] if out_p else None, in_p["utc_ts"], out_p["utc_ts"] if out_p else None,
         in_local, out_local, in_p["tz_name"], overnight, dst, total, regular, overtime,
         1 if shift["complete"] else 0),
    )
    if shift["complete"] and total is not None and total > long_shift:
        _add_issue(conn, "long_shift", "warning", employee_id, in_p["source_id"], work_date,
                   in_p["id"], f"班次时长 {total} 分钟超过阈值 {long_shift} 分钟，请核查",
                   [_raw_ref(conn, in_p["id"]), _raw_ref(conn, out_p["id"])])


def _build_conflicts(conn) -> None:
    """同员工同一工作日出现 >=2 个班次 => 冲突组；解决前这些班次不计汇总。"""
    groups = conn.execute(
        "SELECT employee_id,work_date,COUNT(*) AS c FROM shifts GROUP BY employee_id,work_date HAVING c>1"
    ).fetchall()
    for g in groups:
        existing_group = conn.execute(
            "SELECT id,status FROM conflict_groups WHERE employee_id=? AND work_date=?",
            (g["employee_id"], g["work_date"]),
        ).fetchone()
        if existing_group:
            group_id = existing_group["id"]
        else:
            group_id = conn.execute(
                "INSERT INTO conflict_groups(employee_id,work_date) VALUES(?,?)",
                (g["employee_id"], g["work_date"]),
            ).lastrowid
        shifts = conn.execute(
            "SELECT * FROM shifts WHERE employee_id=? AND work_date=? ORDER BY in_utc",
            (g["employee_id"], g["work_date"]),
        ).fetchall()
        refs = []
        for s in shifts:
            conn.execute("UPDATE shifts SET conflict_group_id=? WHERE id=?", (group_id, s["id"]))
            for pid in (s["in_punch_id"], s["out_punch_id"]):
                if pid:
                    ref = _raw_ref(conn, pid)
                    if ref:
                        refs.append(ref)
        existing_issue = conn.execute(
            "SELECT id,status FROM issues WHERE conflict_group_id=? AND kind='conflict'",
            (group_id,)).fetchone()
        if not existing_issue:
            _add_issue(conn, "conflict", "error", g["employee_id"], shifts[0]["source_id"],
                       g["work_date"], shifts[0]["in_punch_id"],
                       f"员工 {g['employee_id']} 在 {g['work_date']} 存在 {g['c']} 套互斥班次记录，"
                       "解决前不计入工时汇总",
                       refs, conflict_group_id=group_id)


def _excluded_shift_ids(conn) -> set[int]:
    """未解决冲突组中的班次全部排除；已解决则按 resolution 中保留/排除的班次处理。"""
    excluded: set[int] = set()
    for g in conn.execute("SELECT * FROM conflict_groups").fetchall():
        shifts = conn.execute("SELECT id FROM shifts WHERE conflict_group_id=?", (g["id"],)).fetchall()
        ids = [r["id"] for r in shifts]
        if g["status"] == "open":
            excluded.update(ids)
        else:
            resolution = dbmod.loads(g["resolution_json"], {})
            keep = set(resolution.get("keep_shift_ids", []))
            excluded.update(i for i in ids if i not in keep)
    return excluded


def _rebuild_summary(conn, settings: dict) -> None:
    # 重算覆盖当前仍有班次的员工；已无班次的员工旧汇总先整体清空，避免唯一键冲突
    conn.execute("DELETE FROM period_summary")
    period_type = settings["period_type"]
    weekly_anchor = settings["weekly_anchor"]
    excluded = _excluded_shift_ids(conn)
    open_conflict_dates = {
        (r["employee_id"], r["work_date"])
        for r in conn.execute("SELECT employee_id,work_date FROM conflict_groups WHERE status='open'")
    }

    buckets: dict[tuple, dict] = {}
    for s in conn.execute("SELECT * FROM shifts ORDER BY work_date").fetchall():
        if s["id"] in excluded or not s["complete"]:
            continue
        d = datetime.strptime(s["work_date"], "%Y-%m-%d").date()
        key, start, end = timecalc.period_for_date(d, period_type, weekly_anchor)
        bk = (s["employee_id"], key)
        b = buckets.setdefault(bk, {
            "employee_id": s["employee_id"], "period_type": period_type, "period_key": key,
            "period_start": start.isoformat(), "period_end": end.isoformat(),
            "regular": 0, "overtime": 0, "overnight": 0, "dst": 0, "complete": 0,
            "incomplete": 0, "conflicts": 0,
        })
        b["regular"] += s["regular_minutes"] or 0
        b["overtime"] += s["overtime_minutes"] or 0
        b["overnight"] += s["overnight"]
        b["dst"] += s["dst_crossing"]
        b["complete"] += 1

    # 不完整班次与冲突计数（同样不计时，只计数提示）
    for s in conn.execute("SELECT * FROM shifts").fetchall():
        if s["id"] in excluded:
            continue
        d = datetime.strptime(s["work_date"], "%Y-%m-%d").date()
        key, start, end = timecalc.period_for_date(d, period_type, weekly_anchor)
        bk = (s["employee_id"], key)
        b = buckets.setdefault(bk, {
            "employee_id": s["employee_id"], "period_type": period_type, "period_key": key,
            "period_start": start.isoformat(), "period_end": end.isoformat(),
            "regular": 0, "overtime": 0, "overnight": 0, "dst": 0, "complete": 0,
            "incomplete": 0, "conflicts": 0,
        })
        if not s["complete"]:
            b["incomplete"] += 1
        if (s["employee_id"], s["work_date"]) in open_conflict_dates:
            b["conflicts"] += 1

    for b in buckets.values():
        conn.execute(
            "INSERT INTO period_summary(employee_id,period_type,period_key,period_start,period_end,"
            "regular_minutes,overtime_minutes,overnight_shifts,dst_crossing_shifts,complete_shifts,"
            "incomplete_shifts,conflict_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (b["employee_id"], b["period_type"], b["period_key"], b["period_start"], b["period_end"],
             b["regular"], b["overtime"], b["overnight"], b["dst"], b["complete"],
             b["incomplete"], b["conflicts"]),
        )


# ---------- 补卡与冲突处理 ----------

def add_manual_punch(conn, employee_id: str, source_id: int, punch_type: str,
                     local_time: str, reason: str) -> dict:
    source = get_source(conn, source_id)
    fold_policy = conn.execute("SELECT value FROM settings WHERE key='fold_policy'").fetchone()["value"]
    naive = timecalc.parse_local_datetime(local_time)
    resolved = timecalc.resolve_local(naive, source["time_zone"], fold_policy)
    cur = conn.execute(
        "INSERT INTO punches(source_id,batch_id,employee_id,source_emp_code,punch_type,local_ts,"
        "tz_name,utc_ts,fold_policy,manual,note) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
        (source_id, None, employee_id, None, punch_type, resolved.local_iso, source["time_zone"],
         resolved.utc.isoformat(), fold_policy, f"手工补卡: {reason}"),
    )
    punch_id = cur.lastrowid
    conn.execute(
        "INSERT INTO adjustments(employee_id,kind,punch_id,local_ts,tz_name,reason) VALUES(?,?,?,?,?,?)",
        (employee_id, "manual_punch", punch_id, resolved.local_iso, source["time_zone"], reason),
    )
    # 补卡后该员工班次需要重算；仅重建该员工（手工卡保留）
    _recompute_employees(conn, (employee_id,))
    return {"punch_id": punch_id}


def resolve_conflict(conn, group_id: int, keep_shift_ids: list[int], decision: str) -> dict:
    g = conn.execute("SELECT * FROM conflict_groups WHERE id=?", (group_id,)).fetchone()
    if g is None:
        raise KeyError("冲突组不存在")
    conn.execute(
        "UPDATE conflict_groups SET status='resolved',resolution_json=?,resolved_at=datetime('now') WHERE id=?",
        (dbmod.dumps({"keep_shift_ids": keep_shift_ids, "decision": decision}), group_id),
    )
    conn.execute(
        "UPDATE issues SET status='resolved',resolution_json=?,resolved_at=datetime('now') "
        "WHERE conflict_group_id=? AND kind='conflict'",
        (dbmod.dumps({"keep_shift_ids": keep_shift_ids, "decision": decision}), group_id),
    )
    _recompute_employees(conn, (g["employee_id"],))
    return {"group_id": group_id}


def _recompute_employees(conn, employee_ids: tuple[str, ...]) -> None:
    settings = dbmod.get_settings(conn)
    placeholders = ",".join("?" for _ in employee_ids)
    # 重建会重建冲突组；先保存已解决冲突的决议，之后恢复，避免已处理冲突被重新打开
    saved_resolutions = [
        (r["employee_id"], r["work_date"], r["resolution_json"])
        for r in conn.execute(
            f"SELECT employee_id,work_date,resolution_json FROM conflict_groups "
            f"WHERE employee_id IN ({placeholders}) AND status='resolved'", employee_ids)
    ]
    conn.execute(f"DELETE FROM period_summary WHERE employee_id IN ({placeholders})", employee_ids)
    conn.execute(f"DELETE FROM issues WHERE employee_id IN ({placeholders}) AND kind IN "
                 "('missing_punch','unpaired_punch','long_shift','conflict',"
                 "'nonexistent_time','ambiguous_time')", employee_ids)
    conn.execute(f"DELETE FROM shifts WHERE employee_id IN ({placeholders})", employee_ids)
    conn.execute(f"DELETE FROM conflict_groups WHERE employee_id IN ({placeholders})", employee_ids)
    rows = conn.execute(
        f"SELECT * FROM punches WHERE employee_id IN ({placeholders}) "
        "ORDER BY employee_id, source_id, COALESCE(utc_ts,'9999'), id", employee_ids,
    ).fetchall()
    by_employee_source: dict[tuple, list] = {}
    for r in rows:
        by_employee_source.setdefault((r["employee_id"], r["source_id"]), []).append(r)
    long_shift = int(settings["long_shift_minutes"])
    daily_regular = int(settings["daily_regular_minutes"])
    for (employee_id, _src), punches in by_employee_source.items():
        _build_employee_shifts(conn, employee_id, punches, long_shift, daily_regular)
    _build_conflicts(conn)
    # 恢复已解决冲突的状态（_build_conflicts 不会覆盖已存在组的状态，但组是新建的，需要回填）
    for employee_id, work_date, resolution_json in saved_resolutions:
        conn.execute(
            "UPDATE conflict_groups SET status='resolved',resolution_json=?,resolved_at=datetime('now') "
            "WHERE employee_id=? AND work_date=?",
            (resolution_json, employee_id, work_date))
        conn.execute(
            "UPDATE issues SET status='resolved',resolution_json=?,resolved_at=datetime('now') "
            "WHERE employee_id=? AND work_date=? AND kind='conflict'",
            (resolution_json, employee_id, work_date))
    # 冲突组可能重新打开
    _rebuild_summary(conn, settings)


def void_batch(conn, batch_id: int) -> dict:
    batch = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if batch is None:
        raise KeyError("批次不存在")
    conn.execute("UPDATE batches SET status='voided' WHERE id=?", (batch_id,))
    employees = tuple(r["employee_id"] for r in conn.execute(
        "SELECT DISTINCT employee_id FROM punches WHERE batch_id=?", (batch_id,)).fetchall())
    # 先删引用 punches 的派生数据，再删 punches；raw_rows 通过 ON DELETE CASCADE 处理
    conn.execute("DELETE FROM shifts WHERE in_punch_id IN "
                 "(SELECT id FROM punches WHERE batch_id=?) OR out_punch_id IN "
                 "(SELECT id FROM punches WHERE batch_id=?)", (batch_id, batch_id))
    conn.execute("DELETE FROM issues WHERE punch_id IN (SELECT id FROM punches WHERE batch_id=?)",
                 (batch_id,))
    conn.execute("DELETE FROM adjustments WHERE punch_id IN (SELECT id FROM punches WHERE batch_id=?)",
                 (batch_id,))
    conn.execute("DELETE FROM punches WHERE batch_id=?", (batch_id,))
    conn.execute("DELETE FROM raw_rows WHERE batch_id=?", (batch_id,))
    if employees:
        _recompute_employees(conn, employees)
    return {"batch_id": batch_id, "voided": True}
