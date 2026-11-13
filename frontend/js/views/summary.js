// 周期汇总：按员工与薪资周期汇总正常工时、加班、跨夜、DST、缺卡与冲突。
import { api } from "../api.js";
import { chip, clear, el, download, fmtHours, toast } from "../dom.js";

export async function renderSummary(root) {
  const [{ settings }, { employees }] = await Promise.all([api.settings(), api.employees()]);
  const filter = { employee_id: "", period_type: settings.period_type, period_key: "" };

  const empSelect = el("select", {
    onChange: (e) => { filter.employee_id = e.target.value; load(); },
  }, [el("option", { value: "" }, "全部员工"),
      ...employees.map((id) => el("option", { value: id }, id))]);

  const periodType = el("select", {
    onChange: (e) => { filter.period_type = e.target.value; load(); },
  }, [["monthly", "自然月"], ["semimonthly", "半月（1-15 / 16-月末）"], ["weekly", "按周"]]
    .map(([v, t]) => el("option", { value: v, selected: v === filter.period_type }, t)));

  const periodKey = el("input", { placeholder: "周期键，如 2026-03 / 2026-03-上 / W2026-03-16" });
  periodKey.addEventListener("change", () => { filter.period_key = periodKey.value.trim(); load(); });

  const tableHost = el("div");

  async function load() {
    clear(tableHost);
    tableHost.append(el("div", { class: "hint" }, "加载中…"));
    const { summary } = await api.summary(filter);
    clear(tableHost);
    if (!summary.length) {
      tableHost.append(el("div", { class: "empty" }, "没有符合条件的汇总数据。"));
      return;
    }
    const totals = summary.reduce((acc, r) => {
      acc.regular += r.regular_minutes;
      acc.overtime += r.overtime_minutes;
      acc.overnight += r.overnight_shifts;
      acc.dst += r.dst_crossing_shifts;
      acc.incomplete += r.incomplete_shifts;
      acc.conflict += r.conflict_count;
      return acc;
    }, { regular: 0, overtime: 0, overnight: 0, dst: 0, incomplete: 0, conflict: 0 });

    const stat = (num, lbl) => el("div", { class: "stat" }, [
      el("div", { class: "num" }, String(num)), el("div", { class: "lbl" }, lbl)]);

    tableHost.append(
      el("div", { class: "stat-row", style: "margin-bottom:14px" }, [
        stat(fmtHours(totals.regular), "正常工时合计(小时)"),
        stat(fmtHours(totals.overtime), "加班合计(小时)"),
        stat(totals.overnight, "跨夜班次"),
        stat(totals.dst, "DST 跨切换班次"),
        stat(totals.incomplete, "缺卡班次"),
        stat(totals.conflict, "未解决冲突"),
      ]),
      el("div", { class: "table-wrap" }, el("table", {}, [
        el("thead", {}, el("tr", {},
          ["员工", "周期类型", "周期", "区间", "正常(h)", "加班(h)", "合计(h)",
           "跨夜", "DST", "完整班", "缺卡班", "冲突"].map((h) => el("th", {}, h)))),
        el("tbody", {}, summary.map((r) => el("tr", {}, [
          el("td", { class: "mono" }, r.employee_id),
          el("td", {}, r.period_type),
          el("td", { class: "mono small" }, r.period_key),
          el("td", { class: "small mono" }, `${r.period_start} ~ ${r.period_end}`),
          el("td", { class: "right" }, fmtHours(r.regular_minutes)),
          el("td", { class: "right" }, r.overtime_minutes
            ? chip("warning", fmtHours(r.overtime_minutes)) : "0"),
          el("td", { class: "right" }, r.total_hours.toFixed(2)),
          el("td", { class: "center" }, r.overnight_shifts || ""),
          el("td", { class: "center" }, r.dst_crossing_shifts || ""),
          el("td", { class: "center" }, String(r.complete_shifts)),
          el("td", { class: "center" }, r.incomplete_shifts
            ? chip("error", String(r.incomplete_shifts)) : ""),
          el("td", { class: "center" }, r.conflict_count
            ? chip("error", String(r.conflict_count)) : ""),
        ]))),
      ])),
    );
  }

  const btnExport = el("button", { class: "btn primary", onClick: () => {
    const params = Object.fromEntries(
      Object.entries(filter).filter(([, v]) => v));
    download(api.exportUrl.reconciliation(params), "reconciliation.csv");
  }}, "导出核对表 CSV");

  root.append(
    el("div", { class: "panel" }, [
      el("h2", {}, "筛选与周期"),
      el("div", { class: "grid cols-3" }, [
        el("label", { class: "field" }, [el("span", {}, "员工"), empSelect]),
        el("label", { class: "field" }, [el("span", {}, "周期类型"), periodType]),
        el("label", { class: "field" }, [el("span", {}, "周期键（精确匹配）"), periodKey]),
      ]),
      el("div", { class: "btn-row" }, [
        el("button", { class: "btn", onClick: load }, "应用筛选"),
        btnExport,
      ]),
      el("p", { class: "hint" },
        "缺卡班次不计任何工时（不是零工时），仅计数提示；未解决冲突组内班次在解决前不计入汇总。"),
    ]),
    el("div", { class: "panel" }, [el("h2", {}, "汇总结果"), tableHost]),
  );
  await load();
}
