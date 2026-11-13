"""timecalc 的 DST、跨夜、周期与加班规则测试。"""
import unittest
from datetime import datetime

from backend import timecalc


class TimeCalcTest(unittest.TestCase):
    def test_parse_formats(self):
        self.assertEqual(timecalc.parse_local_datetime("2026-03-08 09:30"),
                         datetime(2026, 3, 8, 9, 30))
        self.assertEqual(timecalc.parse_local_datetime("2026/03/08 09:30:00"),
                         datetime(2026, 3, 8, 9, 30))
        self.assertEqual(timecalc.parse_local_datetime("2026年03月08日 09:30"),
                         datetime(2026, 3, 8, 9, 30))
        with self.assertRaises(ValueError):
            timecalc.parse_local_datetime("not-a-time")

    def test_dst_spring_forward_nonexistent(self):
        # 纽约 2026-03-08 02:30 不存在（02:00 -> 03:00）
        r = timecalc.resolve_local(datetime(2026, 3, 8, 2, 30), "America/New_York")
        self.assertEqual(r.kind, "nonexistent")
        # 按跳变前 EST(-5h) 换算 => 07:30 UTC
        self.assertEqual(r.utc, datetime(2026, 3, 8, 7, 30))

    def test_dst_fall_back_ambiguous_first_vs_second(self):
        # 纽约 2026-11-01 01:30 出现两次
        first = timecalc.resolve_local(datetime(2026, 11, 1, 1, 30),
                                       "America/New_York", "first")
        second = timecalc.resolve_local(datetime(2026, 11, 1, 1, 30),
                                        "America/New_York", "second")
        self.assertEqual(first.kind, "ambiguous")
        self.assertEqual(second.kind, "ambiguous")
        # first=EDT(-4) -> 05:30 UTC；second=EST(-5) -> 06:30 UTC，相差一小时
        self.assertEqual(first.utc, datetime(2026, 11, 1, 5, 30))
        self.assertEqual(second.utc, datetime(2026, 11, 1, 6, 30))

    def test_dst_elapsed_hour_difference(self):
        # 春季切换夜 22:00-06:00 跨夜：时钟跳过 1 小时，实际流逝 7 小时（420 分钟）
        tz = "America/New_York"
        start = timecalc.resolve_local(datetime(2026, 3, 7, 22, 0), tz).utc
        end = timecalc.resolve_local(datetime(2026, 3, 8, 6, 0), tz).utc
        self.assertTrue(timecalc.is_dst_crossing(start, end, tz))
        self.assertEqual(timecalc.elapsed_minutes(start, end), 420)

        # 普通夜晚 22:00-06:00 为 8 小时且不跨 DST
        start2 = timecalc.resolve_local(datetime(2026, 3, 14, 22, 0), tz).utc
        end2 = timecalc.resolve_local(datetime(2026, 3, 15, 6, 0), tz).utc
        self.assertFalse(timecalc.is_dst_crossing(start2, end2, tz))
        self.assertEqual(timecalc.elapsed_minutes(start2, end2), 480)

    def test_fall_back_night_is_9_hours(self):
        # 秋季回拨夜 00:00-08:00：区间包含重复的 01 点，时钟回拨 1 小时，实际流逝 9 小时
        tz = "America/New_York"
        start = timecalc.resolve_local(datetime(2026, 11, 1, 0, 0), tz).utc
        end = timecalc.resolve_local(datetime(2026, 11, 1, 8, 0), tz).utc
        self.assertTrue(timecalc.is_dst_crossing(start, end, tz))
        self.assertEqual(timecalc.elapsed_minutes(start, end), 540)

    def test_overnight_work_date(self):
        tz = "Asia/Shanghai"  # 无 DST，便于核对归属日期
        start = timecalc.resolve_local(datetime(2026, 5, 10, 22, 0), tz).utc
        local = timecalc.utc_to_local_iso(start, tz)
        self.assertEqual(local[:10], "2026-05-10")

    def test_periods_monthly_semimonthly_weekly(self):
        d = datetime(2026, 3, 16).date()
        key, start, end = timecalc.period_for_date(d, "monthly")
        self.assertEqual((key, start.day, end.day), ("2026-03", 1, 31))
        key, start, end = timecalc.period_for_date(d, "semimonthly")
        self.assertEqual((key, start.day, end.day), ("2026-03-下", 16, 31))
        key, start, end = timecalc.period_for_date(datetime(2026, 3, 10).date(), "semimonthly")
        self.assertEqual((key, start.day, end.day), ("2026-03-上", 1, 15))
        # 2026-03-16 是周一
        key, start, end = timecalc.period_for_date(d, "weekly", 1)
        self.assertEqual((key, str(start), str(end)),
                         ("W2026-03-16", "2026-03-16", "2026-03-22"))

    def test_overtime_split(self):
        self.assertEqual(timecalc.split_overtime(540, 480), (480, 60))
        self.assertEqual(timecalc.split_overtime(420, 480), (420, 0))


if __name__ == "__main__":
    unittest.main()
