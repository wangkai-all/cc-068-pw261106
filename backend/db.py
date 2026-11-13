"""SQLite 连接与表结构。

所有业务数据仅保存在本地 SQLite 文件中，不连接任何外部系统。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# 允许通过环境变量覆盖数据目录，测试时使用独立目录。
DATA_DIR = Path(os.environ.get("TIMSHEET_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "timesheet.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 数据来源（打卡系统 A / B / 手工表 等）
CREATE TABLE IF NOT EXISTS source_configs (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    format TEXT NOT NULL CHECK(format IN ('csv','tsv','xlsx','json')),
    time_zone TEXT NOT NULL,                 -- IANA 名称，如 Asia/Shanghai
    encoding TEXT NOT NULL DEFAULT 'utf-8-sig',
    options_json TEXT NOT NULL DEFAULT '{}', -- 解析选项（起始行、sheet 名等）
    mapping_json TEXT NOT NULL DEFAULT '{}', -- 字段映射（见 mapping.py）
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 映射修订历史（修订后重算受影响周期时保留可追溯版本）
CREATE TABLE IF NOT EXISTS mapping_versions (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source_configs(id),
    version INTEGER NOT NULL,
    mapping_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_id, version)
);

-- 各来源员工编号 -> 内部员工号
CREATE TABLE IF NOT EXISTS employee_alias (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source_configs(id),
    source_emp_code TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    employee_name TEXT,
    UNIQUE(source_id, source_emp_code)
);

-- 导入的物理文件登记（文件级幂等：相同内容哈希拒绝重复导入）
CREATE TABLE IF NOT EXISTS import_files (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source_configs(id),
    file_name TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_id, content_hash)
);

-- 本地批次（确认写入后生成）
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source_configs(id),
    file_id INTEGER REFERENCES import_files(id),
    code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'committed'
        CHECK(status IN ('committed','recomputed','voided')),
    row_count INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 原始行（逐行留存，支持追溯与冲突时展示原始行）
CREATE TABLE IF NOT EXISTS raw_rows (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    source_id INTEGER NOT NULL REFERENCES source_configs(id),
    file_id INTEGER REFERENCES import_files(id),
    line_no INTEGER NOT NULL,
    row_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(batch_id, row_hash)
);

-- 归一化打卡（每次计算按 raw_rows 全量重建；手工补卡以 manual=1 保留）
CREATE TABLE IF NOT EXISTS punches (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source_configs(id),
    batch_id INTEGER REFERENCES batches(id),
    raw_row_id INTEGER REFERENCES raw_rows(id) ON DELETE CASCADE,
    employee_id TEXT NOT NULL,
    source_emp_code TEXT,
    punch_type TEXT NOT NULL CHECK(punch_type IN ('in','out')),
    local_ts TEXT,            -- 来源时区本地时间 ISO，缺卡时为 NULL
    tz_name TEXT NOT NULL,
    utc_ts TEXT,              -- UTC ISO，缺卡时为 NULL
    fold_policy TEXT NOT NULL DEFAULT 'first',
    manual INTEGER NOT NULL DEFAULT 0,
    adjustment_id INTEGER,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_punches_emp_utc ON punches(employee_id, utc_ts);
CREATE INDEX IF NOT EXISTS idx_punches_raw ON punches(raw_row_id);

-- 计算出的班次（派生数据，重算时重建）
CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY,
    employee_id TEXT NOT NULL,
    source_id INTEGER NOT NULL REFERENCES source_configs(id),
    work_date TEXT NOT NULL,         -- 上班打卡的本地日期
    in_punch_id INTEGER REFERENCES punches(id) ON DELETE CASCADE,
    out_punch_id INTEGER REFERENCES punches(id) ON DELETE CASCADE,
    in_utc TEXT, out_utc TEXT,
    in_local TEXT, out_local TEXT,
    tz_name TEXT NOT NULL,
    overnight INTEGER NOT NULL DEFAULT 0,
    dst_crossing INTEGER NOT NULL DEFAULT 0,
    total_minutes INTEGER,           -- 缺卡未补时为 NULL（绝不按 0 计）
    regular_minutes INTEGER,
    overtime_minutes INTEGER,
    complete INTEGER NOT NULL DEFAULT 0,
    conflict_group_id INTEGER,
    UNIQUE(employee_id, source_id, in_punch_id)
);
CREATE INDEX IF NOT EXISTS idx_shifts_emp_date ON shifts(employee_id, work_date);

-- 问题清单（缺卡 / 时间不存在 / 可疑长班次 / 冲突 / 重复）
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN (
        'missing_punch','nonexistent_time','ambiguous_time',
        'unpaired_punch','long_shift','conflict','duplicate_row')),
    severity TEXT NOT NULL DEFAULT 'warning' CHECK(severity IN ('error','warning','info')),
    employee_id TEXT,
    source_id INTEGER,
    work_date TEXT,
    punch_id INTEGER REFERENCES punches(id) ON DELETE CASCADE,
    shift_id INTEGER REFERENCES shifts(id) ON DELETE CASCADE,
    conflict_group_id INTEGER REFERENCES conflict_groups(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    raw_refs_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved','ignored')),
    resolution_json TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS idx_issues_emp ON issues(employee_id, work_date);

-- 同员工同日冲突分组
CREATE TABLE IF NOT EXISTS conflict_groups (
    id INTEGER PRIMARY KEY,
    employee_id TEXT NOT NULL,
    work_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
    resolution_json TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(employee_id, work_date)
);

-- 手工补卡 / 修订记录
CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY,
    employee_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('missing_punch_fix','manual_punch','override')),
    punch_id INTEGER REFERENCES punches(id) ON DELETE CASCADE,
    local_ts TEXT,
    tz_name TEXT,
    reason TEXT,
    operator TEXT NOT NULL DEFAULT 'local-user',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 周期汇总（派生缓存，重算时重建）
CREATE TABLE IF NOT EXISTS period_summary (
    id INTEGER PRIMARY KEY,
    employee_id TEXT NOT NULL,
    period_type TEXT NOT NULL,
    period_key TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    regular_minutes INTEGER NOT NULL DEFAULT 0,
    overtime_minutes INTEGER NOT NULL DEFAULT 0,
    overnight_shifts INTEGER NOT NULL DEFAULT 0,
    dst_crossing_shifts INTEGER NOT NULL DEFAULT 0,
    complete_shifts INTEGER NOT NULL DEFAULT 0,
    incomplete_shifts INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(employee_id, period_type, period_key)
);
CREATE INDEX IF NOT EXISTS idx_period ON period_summary(period_type, period_key);

-- 计算运行记录（每次全量/范围重算留痕）
CREATE TABLE IF NOT EXISTS build_runs (
    id INTEGER PRIMARY KEY,
    scope_json TEXT NOT NULL,
    shifts_built INTEGER NOT NULL DEFAULT 0,
    issues_open INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 全局键值设置（周期类型、加班阈值等）
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "period_type": "monthly",          # monthly | semimonthly | weekly
    "weekly_anchor": "1",             # ISO 周一=1
    "daily_regular_minutes": "480",   # 每日正常工时上限，超出计加班
    "long_shift_minutes": "960",      # 超过 16 小时标记可疑长班次
    "fold_policy": "first",           # DST 回拨模糊时间：first(夏令时实例) | second
}


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO NOTHING",
            (key, value),
        )
    conn.commit()


@contextmanager
def session(db_path: Path | str | None = None):
    conn = connect(db_path)
    try:
        init_db(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- 通用 JSON 辅助 ----------

def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value, default=None):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def get_settings(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key,value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}
