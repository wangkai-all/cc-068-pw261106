"""时间、夏令时与薪资周期的纯函数计算。

规则约定（界面与 README 中同步说明）：

1. 所有打卡先按来源配置的 IANA 时区解释为本地时间，再转换为 UTC 存储；
   班次时长一律按 UTC 实际流逝分钟计算，因此 DST 切换日天然得到 60/180 分钟的差异。
2. 春季跳变（时间不存在，如 02:00→03:00）：按跳变前偏移量换算到 UTC，并记录
   nonexistent_time 问题，绝不静默改时间。
3. 秋季回拨（模糊时间，如 01:00 出现两次）：按 fold_policy 处理，
   first=第一次（夏令时实例，fold=0，默认），second=第二次（冬令时实例，fold=1），
   并记录 ambiguous_time 问题。
4. 跨夜班次：以上班打卡的本地日期作为归属工作日 work_date；下班本地日期更晚即标记 overnight。
5. 缺少下班（或上班）时间：班次 total_minutes 为 NULL，不计任何工时，只产生 missing_punch，
   绝不按零工时处理。补卡后重算才计入。
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LOCAL_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
    "%d.%m.%Y %H:%M",
    "%m/%d/%Y %H:%M",   # 美制月/日/年
    "%d/%m/%Y %H:%M",   # 欧制日/月/年
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
)
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%d.%m.%Y", "%m/%d/%Y", "%d/%m/%Y")
TIME_FORMATS = ("%H:%M:%S", "%H:%M")


def parse_local_datetime(text: str) -> datetime:
    """把多种常见文本解析为 naive datetime（不带时区）。"""
    if text is None:
        raise ValueError("空时间")
    s = str(text).strip()
    if not s:
        raise ValueError("空时间")
    # 兼容 ISO 中带 Z/偏移的写法：先归一为 naive（来源本身另有时区配置，此处偏移仅作容错）
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    for fmt in LOCAL_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析的时间格式: {text!r}")


def parse_date(text: str) -> date:
    s = str(text).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"无法解析的日期: {text!r}")


def parse_time(text: str) -> tuple[int, int]:
    s = str(text).strip()
    for fmt in TIME_FORMATS:
        try:
            t = datetime.strptime(s, fmt)
            return t.hour, t.minute
        except ValueError:
            continue
    raise ValueError(f"无法解析的时间: {text!r}")


def combine_date_time(date_text: str, time_text: str) -> datetime:
    """来源把日期与时间拆成两列时使用。"""
    d = parse_date(date_text)
    hour, minute = parse_time(time_text)
    return datetime(d.year, d.month, d.day, hour, minute)


@dataclass
class LocalResolve:
    utc: datetime | None          # timezone.utc 下的 datetime；输入为 None 时为 None
    kind: str                     # ok | nonexistent | ambiguous
    local_iso: str | None
    offset_minutes: int | None


def resolve_local(naive: datetime | None, tz_name: str, fold_policy: str = "first") -> LocalResolve:
    """本地 naive 时间 -> UTC，同时判定 DST 缺口/重叠。"""
    if naive is None:
        return LocalResolve(None, "ok", None, None)
    tz = ZoneInfo(tz_name)
    dt0 = naive.replace(tzinfo=tz, fold=0)
    dt1 = naive.replace(tzinfo=tz, fold=1)
    off0 = dt0.utcoffset()
    off1 = dt1.utcoffset()
    if off0 == off1:
        kind = "ok"
        aware = dt0
    elif off0 < off1:
        # 春季跳变：本地时间不存在。规则：按跳变前偏移（fold=0）换算并标记问题。
        kind = "nonexistent"
        aware = dt0
    else:
        # 秋季回拨：模糊时间。first=fold0（夏令时实例），second=fold1（冬令时实例）。
        kind = "ambiguous"
        aware = dt0 if fold_policy == "first" else dt1
    utc = aware.astimezone(timezone.utc).replace(tzinfo=None)
    return LocalResolve(
        utc=utc,
        kind=kind,
        local_iso=naive.isoformat(),
        offset_minutes=int(aware.utcoffset().total_seconds()) // 60,
    )


def utc_to_local_iso(utc_naive: datetime | None, tz_name: str) -> str | None:
    if utc_naive is None:
        return None
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name)).isoformat()


def is_dst_crossing(in_utc: datetime, out_utc: datetime, tz_name: str) -> bool:
    """班次两端的 UTC 偏移不同，即班次跨越了 DST 切换点。"""
    tz = ZoneInfo(tz_name)
    off_in = in_utc.replace(tzinfo=timezone.utc).astimezone(tz).utcoffset()
    off_out = out_utc.replace(tzinfo=timezone.utc).astimezone(tz).utcoffset()
    return off_in != off_out


def elapsed_minutes(in_utc: datetime, out_utc: datetime) -> int:
    """按 UTC 实际流逝分钟计时（DST 切换日自动得到 60/180 分钟差异）。"""
    delta = out_utc - in_utc
    return int(delta.total_seconds()) // 60


# ---------- 薪资周期 ----------

PERIOD_MONTHLY = "monthly"
PERIOD_SEMIMONTHLY = "semimonthly"
PERIOD_WEEKLY = "weekly"


def month_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def period_for_date(d: date, period_type: str, weekly_anchor: int = 1) -> tuple[str, date, date]:
    """返回 (period_key, start, end)，end 含当天。"""
    if period_type == PERIOD_MONTHLY:
        start, end = month_bounds(d.year, d.month)
        return f"{d.year:04d}-{d.month:02d}", start, end

    if period_type == PERIOD_SEMIMONTHLY:
        if d.day <= 15:
            start = date(d.year, d.month, 1)
            end = date(d.year, d.month, 15)
            key = f"{d.year:04d}-{d.month:02d}-上"
        else:
            start = date(d.year, d.month, 16)
            end = month_bounds(d.year, d.month)[1]
            key = f"{d.year:04d}-{d.month:02d}-下"
        return key, start, end

    if period_type == PERIOD_WEEKLY:
        anchor = int(weekly_anchor) % 7  # 周日存 0
        shift = (d.isoweekday() - anchor) % 7
        start = d - timedelta(days=shift)
        end = start + timedelta(days=6)
        return f"W{start.isoformat()}", start, end

    raise ValueError(f"未知周期类型: {period_type}")


def split_overtime(total_minutes: int, daily_regular_minutes: int) -> tuple[int, int]:
    """按日规则拆分正常/加班：日工时超过正常阈值的部分计加班。"""
    if total_minutes is None:
        return 0, 0
    regular = min(total_minutes, int(daily_regular_minutes))
    overtime = max(0, total_minutes - int(daily_regular_minutes))
    return regular, overtime
