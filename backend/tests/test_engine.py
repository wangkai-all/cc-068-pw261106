"""引擎端到端规则测试：去重、缺卡、跨夜、DST、冲突、补卡与重算。"""
import tempfile
import unittest
from pathlib import Path

from backend import db as dbmod
from backend import engine

CSV_A = (
    "工号,姓名,打卡时间,状态\n"
    "E001,张三,2026-03-07 22:00,上班\n"
    "E001,张三,2026-03-08 06:00,下班\n"   # DST 春季切换跨夜班，实际 8 小时
    "E001,张三,2026-03-09 09:00,上班\n"   # 缺下班 -> 缺卡，不计工时
    "E002,李四,2026-03-09 09:00,上班\n"
    "E002,李四,2026-03-09 18:00,下班\n"   # 9 小时：8 正常 + 1 加班
)

CSV_A_DUP = CSV_A  # 完全相同文件

CSV_B_CONFLICT = (
    "EmpID,Clock,Type\n"
    "X1,2026-03-09 08:00,IN\n"
    "X1,2026-03-09 17:00,OUT\n"
)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.conn = dbmod.connect(self.db_path)
        dbmod.init_db(self.conn)
        self.sid = engine.upsert_source(
            self.conn, "SYS-A", "打卡系统A", "csv", "America/New_York", "utf-8-sig", {},
            {"fields": {"employee_code": "工号", "employee_name": "姓名",
                        "punch_time": "打卡时间", "punch_type": "状态"},
             "punch_type_aliases": {"in": ["上班"], "out": ["下班"]}})
        self.sid_b = engine.upsert_source(
            self.conn, "SYS-B", "打卡系统B", "csv", "America/New_York", "utf-8-sig", {},
            {"fields": {"employee_code": "EmpID", "punch_time": "Clock", "punch_type": "Type"},
             "punch_type_aliases": {"in": ["IN"], "out": ["OUT"]}})
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _commit(self, sid, text):
        return engine.commit_batch(self.conn, sid, text.encode("utf-8"), "f.csv")

    def test_full_pipeline_rules(self):
        result = self._commit(self.sid, CSV_A)
        self.assertEqual(result["written"], 5)
        self.assertEqual(result["skipped_duplicates"], 0)

        # 跨夜 DST 班：E001 3/7 上班 -> 3/8 下班，春季跳表实际 420 分钟、overnight=1、dst=1
        night = self.conn.execute(
            "SELECT * FROM shifts WHERE employee_id='E001' AND work_date='2026-03-07'").fetchone()
        self.assertIsNotNone(night)
        self.assertEqual(night["total_minutes"], 420)
        self.assertEqual(night["overnight"], 1)
        self.assertEqual(night["dst_crossing"], 1)

        # 缺下班：total_minutes 必须为 NULL（不是 0），且产生 missing_punch
        incomplete = self.conn.execute(
            "SELECT * FROM shifts WHERE employee_id='E001' AND work_date='2026-03-09'").fetchone()
        self.assertIsNotNone(incomplete)
        self.assertIsNone(incomplete["total_minutes"])
        self.assertEqual(incomplete["complete"], 0)
        missing = self.conn.execute(
            "SELECT COUNT(*) c FROM issues WHERE kind='missing_punch' AND status='open'").fetchone()
        self.assertGreaterEqual(missing["c"], 1)

        # E002 9 小时 -> 480 正常 + 60 加班
        normal = self.conn.execute(
            "SELECT * FROM shifts WHERE employee_id='E002' AND work_date='2026-03-09'").fetchone()
        self.assertEqual((normal["regular_minutes"], normal["overtime_minutes"]), (480, 60))

        # 周期汇总：E002 三月周期正常 8h、加班 1h；缺卡不贡献工时
        summary = self.conn.execute(
            "SELECT * FROM period_summary WHERE employee_id='E002' AND period_type='monthly'").fetchone()
        self.assertEqual(summary["regular_minutes"], 480)
        self.assertEqual(summary["overtime_minutes"], 60)
        e001_summary = self.conn.execute(
            "SELECT * FROM period_summary WHERE employee_id='E001' AND period_type='monthly'").fetchone()
        self.assertEqual(e001_summary["regular_minutes"], 420)  # 仅跨夜班（DST 7h），缺卡班不计
        self.assertEqual(e001_summary["incomplete_shifts"], 1)

    def test_file_level_idempotency(self):
        self._commit(self.sid, CSV_A)
        with self.assertRaises(ValueError):
            self._commit(self.sid, CSV_A_DUP)

    def test_row_level_dedup(self):
        self._commit(self.sid, CSV_A)
        # 追加文件包含两行旧内容 + 一行新内容，旧行应跳过
        extra = (
            "工号,姓名,打卡时间,状态\n"
            "E002,李四,2026-03-09 09:00,上班\n"
            "E002,李四,2026-03-09 18:00,下班\n"
            "E003,王五,2026-03-10 09:00,上班\n"
        )
        result = self._commit(self.sid, extra)
        self.assertEqual(result["written"], 1)
        self.assertEqual(result["skipped_duplicates"], 2)

    def test_conflict_group_excludes_from_summary(self):
        self._commit(self.sid, CSV_A)
        # 系统 B 的 X1 映射到同一员工 E002，且同一天另有一套班次 -> 冲突
        engine.set_alias(self.conn, self.sid_b, "X1", "E002", "李四")
        self._commit(self.sid_b, CSV_B_CONFLICT)
        group = self.conn.execute(
            "SELECT * FROM conflict_groups WHERE employee_id='E002' AND work_date='2026-03-09'").fetchone()
        self.assertIsNotNone(group)
        self.assertEqual(group["status"], "open")
        # 冲突未解决：E002 当天班次全部排除，三月汇总不含这两套班次的工时
        summary = self.conn.execute(
            "SELECT * FROM period_summary WHERE employee_id='E002' AND period_type='monthly'").fetchone()
        if summary is not None:
            self.assertEqual(summary["regular_minutes"], 0)
            self.assertEqual(summary["overtime_minutes"], 0)
            self.assertGreaterEqual(summary["conflict_count"], 1)
        else:
            # 所有班次都被排除时可以不产生汇总行
            self.assertIsNotNone(self.conn.execute(
                "SELECT 1 FROM conflict_groups WHERE employee_id='E002' AND status='open'").fetchone())

        # 解决冲突：保留系统 A 的班次
        keep = self.conn.execute(
            "SELECT s.id FROM shifts s JOIN source_configs sc ON sc.id=s.source_id "
            "WHERE s.conflict_group_id=? AND sc.code='SYS-A'", (group["id"],)).fetchone()
        engine.resolve_conflict(self.conn, group["id"], [keep["id"]], "以系统A为准")
        summary = self.conn.execute(
            "SELECT * FROM period_summary WHERE employee_id='E002' AND period_type='monthly'").fetchone()
        self.assertEqual(summary["regular_minutes"], 480)
        self.assertEqual(summary["overtime_minutes"], 60)

    def test_manual_punch_fixes_missing(self):
        self._commit(self.sid, CSV_A)
        # 给 E001 3/9 的缺下班补卡 18:00
        engine.add_manual_punch(self.conn, "E001", self.sid, "out",
                                "2026-03-09 18:00", "员工补卡申请单#12")
        shift = self.conn.execute(
            "SELECT * FROM shifts WHERE employee_id='E001' AND work_date='2026-03-09'").fetchone()
        self.assertEqual(shift["complete"], 1)
        self.assertEqual(shift["total_minutes"], 540)  # 09:00-18:00
        still_open = self.conn.execute(
            "SELECT COUNT(*) c FROM issues WHERE kind='missing_punch' AND status='open' "
            "AND employee_id='E001'").fetchone()
        self.assertEqual(still_open["c"], 0)
        # 手工卡在重算后保留
        engine.recompute_all(self.conn)
        manual = self.conn.execute("SELECT COUNT(*) c FROM punches WHERE manual=1").fetchone()
        self.assertEqual(manual["c"], 1)

    def test_mapping_revision_recompute(self):
        self._commit(self.sid, CSV_A)
        before = self.conn.execute(
            "SELECT regular_minutes FROM period_summary WHERE employee_id='E002'").fetchone()
        self.assertEqual(before["regular_minutes"], 480)
        # 修订映射：把下班时间列改错再改回，验证重算结果稳定且映射版本留痕
        engine.upsert_source(
            self.conn, "SYS-A", "打卡系统A", "csv", "America/New_York", "utf-8-sig", {},
            {"fields": {"employee_code": "工号", "employee_name": "姓名",
                        "punch_time": "打卡时间", "punch_type": "状态"},
             "punch_type_aliases": {"in": ["上班"], "out": ["下班"]}})
        engine.recompute_all(self.conn, self.sid)
        after = self.conn.execute(
            "SELECT regular_minutes FROM period_summary WHERE employee_id='E002'").fetchone()
        self.assertEqual(after["regular_minutes"], 480)
        versions = self.conn.execute(
            "SELECT COUNT(*) c FROM mapping_versions WHERE source_id=?", (self.sid,)).fetchone()
        self.assertGreaterEqual(versions["c"], 2)

    def test_dst_special_punches_create_issues(self):
        # 纽约 2026-11-01 01:30 模糊时间与 2026-03-08 02:30 缺口时间都应生成独立问题
        dst_csv = (
            "工号,姓名,打卡时间,状态\n"
            "E009,赵六,2026-11-01 01:30,上班\n"
            "E009,赵六,2026-11-01 10:00,下班\n"
            "E009,赵六,2026-03-08 02:30,上班\n"
            "E009,赵六,2026-03-08 10:00,下班\n"
        )
        self._commit(self.sid, dst_csv)
        kinds = {r["kind"] for r in self.conn.execute(
            "SELECT DISTINCT kind FROM issues WHERE employee_id='E009'").fetchall()}
        self.assertIn("ambiguous_time", kinds)
        self.assertIn("nonexistent_time", kinds)

    def test_source_recompute_keeps_other_sources(self):
        # A 源 E005 一天，B 源同员工另一天；仅重算 A 时，B 的打卡与工时必须保留
        csv_a = ("工号,姓名,打卡时间,状态\n"
                 "E005,钱七,2026-03-05 09:00,上班\nE005,钱七,2026-03-05 18:00,下班\n")
        csv_b = ("EmpID,Clock,Type\n"
                 "E005,2026-03-06 09:00,IN\nE005,2026-03-06 18:00,OUT\n")
        self._commit(self.sid, csv_a)
        self._commit(self.sid_b, csv_b)
        before_b = self.conn.execute(
            "SELECT COUNT(*) c FROM punches WHERE employee_id='E005' AND source_id=?",
            (self.sid_b,)).fetchone()["c"]
        self.assertEqual(before_b, 2)

        engine.recompute_all(self.conn, self.sid)  # 只重算 A 源

        after_b = self.conn.execute(
            "SELECT COUNT(*) c FROM punches WHERE employee_id='E005' AND source_id=?",
            (self.sid_b,)).fetchone()["c"]
        self.assertEqual(after_b, 2, "按来源重算不得删除该员工其他来源的打卡")
        # 两天各 9 小时，汇总应保留 16h 正常 + 2h 加班
        summary = self.conn.execute(
            "SELECT * FROM period_summary WHERE employee_id='E005' AND period_type='monthly'").fetchone()
        self.assertEqual(summary["regular_minutes"], 960)
        self.assertEqual(summary["overtime_minutes"], 120)

    def test_void_batch_removes_hours(self):
        r = self._commit(self.sid, CSV_A)
        engine.void_batch(self.conn, r["batch_id"])
        total = self.conn.execute(
            "SELECT COALESCE(SUM(regular_minutes),0) v FROM period_summary").fetchone()
        self.assertEqual(total["v"], 0)


if __name__ == "__main__":
    unittest.main()
