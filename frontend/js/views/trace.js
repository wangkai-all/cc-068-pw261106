// 员工追溯：按员工列出每条打卡对应的原始行、批次与计算出的班次，并可导出。
import { api } from "../api.js";
import { chip, clear, el, download, fmtHours, modal } from "../dom.js";

export async function renderTrace(root) {
  const { employees } = await api.employees();
  const empSel = el("select", {}, [
    el("option", { value: "" }, "选择员工…"),
    ...employees.map((id) => el("option", { value: id }, id)),
  ]);
  const host = el("div");

  async function load(employeeId) {
    clear(host);
    if (!employeeId) {
      host.append(el("div", { class: "empty" }, "请选择员工。"));
      return;
    }
    const { trace } = await api.trace(employeeId);
    if (!trace.punches.length) {
      host.append(el("div", { class: "empty" }, "该员工没有打卡记录。"));
      return;
    }
    host.append(
      el("div", { class: "btn-row" }, [
        el("button", { class: "btn primary",
          onClick: () => download(api.exportUrl.trace(employeeId), `trace_${employeeId}.csv`) },
          "导出该员工追溯 CSV"),
      ]),
      el("div", { class: "table-wrap", style: "margin-top:12px" }, el("table", {}, [
        el("thead", {}, el("tr", {},
          ["来源", "批次", "原始行", "类型", "本地时间", "UTC", "手工", "归属工作日",
           "班次工时", "跨夜", "DST", "原始行内容"].map((h) => el("th", {}, h)))),
        el("tbody", {}, trace.punches.map((p) => el("tr", {}, [
          el("td", { class: "small" }, p.source_code),
          el("td", { class: "small mono" }, p.batch_code || (p.manual ? "手工补卡" : "—")),
          el("td", { class: "center" }, p.line_no || "—"),
          el("td", {}, chip("neutral", p.punch_type)),
          el("td", { class: "mono small" }, p.local_ts || chip("error", "缺卡")),
          el("td", { class: "mono small" }, p.utc_ts || "—"),
          el("td", {}, p.manual ? chip("info", "手工") : ""),
          el("td", { class: "mono small" }, p.shift?.work_date || "—"),
          el("td", {}, p.shift
            ? (p.shift.total_minutes == null
                ? chip("error", "未计时")
                : `${fmtHours(p.shift.total_minutes)} h`)
            : "—"),
          el("td", {}, p.shift?.overnight ? chip("info", "跨夜") : ""),
          el("td", {}, p.shift?.dst_crossing ? chip("warning", "DST") : ""),
          el("td", {}, p.raw
            ? el("button", { class: "btn small",
                onClick: () => modal(`原始行 · ${p.employee_id}`,
                  el("pre", { class: "raw" }, JSON.stringify(p.raw, null, 2))) }, "查看")
            : chip("muted", "无（手工）")),
        ]))),
      ])),
    );
  }

  empSel.addEventListener("change", () => load(empSel.value));
  root.append(
    el("div", { class: "panel" }, [
      el("h2", {}, "按员工追溯原始行"),
      el("label", { class: "field", style: "max-width:360px" }, [el("span", {}, "员工"), empSel]),
      el("p", { class: "hint" },
        "每条归一化打卡都可回溯到来源文件的原始行与批次；手工补卡单独标记。缺卡行显示“缺卡/未计时”，不会出现零工时。"),
    ]),
    el("div", { class: "panel" }, host),
  );
  await load("");
}
