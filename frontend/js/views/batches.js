// 本地批次：列出已确认写入的批次，支持作废（作废后自动重算受影响员工）。
import { api } from "../api.js";
import { chip, clear, el, toast } from "../dom.js";

const STATUS = {
  committed: ["success", "已提交"],
  recomputed: ["neutral", "已重算"],
  voided: ["muted", "已作废"],
};

export async function renderBatches(root) {
  const { batches } = await api.listBatches();
  const table = batches.length
    ? el("table", {}, [
        el("thead", {}, el("tr", {},
          ["批次号", "来源", "文件名", "行数", "状态", "备注", "创建时间", "操作"].map((h) => el("th", {}, h)))),
        el("tbody", {}, batches.map((b) => el("tr", {
          style: b.status === "voided" ? "opacity:0.55" : "",
        }, [
          el("td", { class: "mono small" }, b.code),
          el("td", {}, `${b.source_code} · ${b.source_name}`),
          el("td", { class: "small" }, b.file_name || "—"),
          el("td", {}, String(b.row_count)),
          el("td", {}, chip(...STATUS[b.status] || ["muted", b.status])),
          el("td", { class: "small" }, b.note || "—"),
          el("td", { class: "small mono" }, b.created_at),
          el("td", {}, b.status === "voided" ? "—" : el("button", {
            class: "btn danger small",
            onClick: async () => {
              if (!confirm(`确认作废批次 ${b.code}？该批次原始行与打卡将被移除并自动重算。`)) return;
              try {
                await api.voidBatch(b.id);
                toast("批次已作废，相关周期已重算");
                clear(root);
                renderBatches(root);
              } catch (exc) {
                toast(exc.message, false);
              }
            },
          }, "作废并重算")),
        ]))),
      ])
    : el("div", { class: "empty" }, "还没有批次，请先在「导入与预览」中确认写入。");

  root.append(el("div", { class: "panel" }, [
    el("h2", {}, "本地批次"),
    el("p", { class: "hint" }, "批次是确认写入后的不可变入账单元；作废会移除其原始行与归一化打卡，并对受影响员工重新计算。"),
    el("div", { class: "table-wrap" }, table),
  ]));
}
