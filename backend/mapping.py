"""字段映射配置与原始行 -> 归一化打卡的转换。

mapping_json 结构示例：
{
  "fields": {
    "employee_code": "工号",
    "punch_time": "打卡时间",
    "punch_date": null,
    "punch_clock": null,
    "punch_type": "状态",
    "employee_name": "姓名"
  },
  "punch_type_aliases": {
    "in": ["上班", "IN", "签到", "check-in"],
    "out": ["下班", "OUT", "签退", "check-out"]
  }
}

punch_time 单列，或 punch_date + punch_clock 两列组合，二选一。
"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field

from . import timecalc

CANONICAL_FIELDS = ("employee_code", "employee_name", "punch_time", "punch_date", "punch_clock", "punch_type")


def default_mapping(columns: list[str]) -> dict:
    """根据列名做一次大小写/空格无关的猜测，生成初始映射供界面调整。"""
    normalized = {c.strip().lower(): c for c in columns if c is not None}
    aliases = {
        "employee_code": ("employee_code", "emp_code", "emp id", "empid", "id", "工号", "员工编号", "员工号", "编号"),
        "employee_name": ("employee_name", "name", "姓名", "员工姓名", "名字"),
        "punch_time": ("punch_time", "time", "timestamp", "datetime", "打卡时间", "考勤时间", "时间"),
        "punch_date": ("punch_date", "date", "日期", "打卡日期"),
        "punch_clock": ("punch_clock", "clock", "time", "时间", "打卡时刻"),
        "punch_type": ("punch_type", "type", "status", "状态", "类型", "打卡类型"),
    }

    def guess(names: tuple[str, ...]):
        for name in names:
            if name in normalized:
                return normalized[name]
        return None

    fields_map = {key: guess(names) for key, names in aliases.items()}
    return {
        "fields": fields_map,
        "punch_type_aliases": {
            "in": ["上班", "IN", "签到", "check-in", "CheckIn"],
            "out": ["下班", "OUT", "签退", "check-out", "CheckOut"],
        },
    }


def get_field(mapping: dict, key: str) -> str | None:
    return (mapping.get("fields") or {}).get(key)


def _cell(row: dict, column: str | None):
    if not column:
        return None
    value = row.get(column)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_punch_type(raw_type: str | None, aliases: dict) -> str | None:
    if raw_type is None:
        return None
    needle = raw_type.strip().lower()
    for canonical in ("in", "out"):
        for alias in aliases.get(canonical, []):
            if needle == str(alias).strip().lower():
                return canonical
    return None


@dataclass
class NormalizedPunch:
    line_no: int
    raw: dict
    source_emp_code: str | None = None
    employee_name: str | None = None
    punch_type: str | None = None
    local_naive_iso: str | None = None       # 解析成功的本地时间；缺卡为 None
    tz_name: str = ""
    fold_policy: str = "first"
    row_hash: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dst_kind: str = "ok"                      # ok | nonexistent | ambiguous


def row_hash(row: dict) -> str:
    """对原始行内容做稳定哈希；同内容行（无论出现在哪个文件）哈希相同。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["" if row.get(k) is None else str(row.get(k)) for k in sorted(row.keys())])
    return hashlib.sha256(buf.getvalue().encode("utf-8")).hexdigest()


def normalize_row(row: dict, line_no: int, mapping: dict, tz_name: str, fold_policy: str = "first") -> NormalizedPunch:
    """按映射转换一行。解析问题收集到 errors/warnings，不抛异常，保证整批可预览。"""
    p = NormalizedPunch(line_no=line_no, raw=dict(row), tz_name=tz_name, fold_policy=fold_policy)
    p.row_hash = row_hash(row)
    p.source_emp_code = _cell(row, get_field(mapping, "employee_code"))
    p.employee_name = _cell(row, get_field(mapping, "employee_name"))
    raw_type = _cell(row, get_field(mapping, "punch_type"))
    p.punch_type = normalize_punch_type(raw_type, mapping.get("punch_type_aliases", {}))

    if not p.source_emp_code:
        p.errors.append("缺少员工编号或映射列错误")
    if raw_type is None:
        p.warnings.append("缺少打卡类型列，将依赖配对顺序推断")
    elif p.punch_type is None:
        p.errors.append(f"无法识别的打卡类型: {raw_type!r}")

    # 时间：单列 或 日期+时刻两列
    time_col = get_field(mapping, "punch_time")
    date_col = get_field(mapping, "punch_date")
    clock_col = get_field(mapping, "punch_clock")
    raw_time = _cell(row, time_col)
    raw_date = _cell(row, date_col)
    raw_clock = _cell(row, clock_col)

    naive = None
    if raw_time:
        try:
            naive = timecalc.parse_local_datetime(raw_time)
        except ValueError as exc:
            p.errors.append(str(exc))
    elif raw_date and raw_clock:
        try:
            naive = timecalc.combine_date_time(raw_date, raw_clock)
        except ValueError as exc:
            p.errors.append(str(exc))
    elif raw_date or raw_clock:
        p.errors.append("日期与时刻必须同时提供")
    else:
        # 明确的缺卡：不是零工时，后续生成 missing_punch 问题
        p.warnings.append("缺少打卡时间（按缺卡处理，不计工时）")

    if naive is not None:
        resolved = timecalc.resolve_local(naive, tz_name, fold_policy)
        p.local_naive_iso = resolved.local_iso
        p.dst_kind = resolved.kind
        if resolved.kind == "nonexistent":
            p.warnings.append("打卡时间落在 DST 春季跳变缺口，按跳变前偏移换算并标记")
        elif resolved.kind == "ambiguous":
            p.warnings.append(f"打卡时间为 DST 秋季回拨模糊时间，按 {fold_policy} 规则解释")
    return p
