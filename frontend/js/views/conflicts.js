// 冲突处理：同员工同一工作日出现多套互斥班次，列出每套班次的原始行，人工选择保留。
import { api } from "../api.js";
import { chip, clear, el, download, modal, toast } from "../dom.js";

function rawBlock(title, raw) {
  return el("div", {}, [
    el("div", { class: "small", style: "font-weight:600;margin-top:6px" }, title),
    el("pre", { class: "raw" }, JSON.stringify(raw || "（手工补卡，无原始行）", null, 2)),
  ]);
}

export async function renderConflicts(root) {
  const host = el("div");

  async function load() {
    clear(host);
    const { conflicts } = await api.conflicts();
    if (!conflicts.length) {
      host.append(el("div", { class: "empty" }, "当前没有冲突记录。"));
      return;
    }
    host.append(...conflicts.map((g) => {
      const keep = new Set((g.resolution || {}).keep_shift_ids || []);
      const choices = g.shifts.map((s) => {
        const radio = el("input", {
          type: "radio", name: `g${g.id}`, value: String(s.id),
          checked: g.status === "resolved" && keep.has(s.id),
          disabled: g.status === "resolved",
        });
        return el("tr", {}, [
          el("td", {}, radio),
          el("td", {}, chip("neutral", s.source_code)),
          el("td", { class: "mono small" }, s.in_local || "—"),
          el("td", { class: "mono small" }, s.out_local || chip("error", "缺下班")),
          el("td", {}, s.total_minutes == null
            ? chip("error", "未计时")
            : `${(s.total_minutes / 60).toFixed(2)} h`),
          el("td", {}, [
            s.overnight ? chip("info", "跨夜") : "",
            s.dst_crossing ? chip("warning", "DST") : "",
          ].filter(Boolean)),
          el("td", {}, el("button", {
            class: "btn small",
            onClick: () => modal(`原始行 · 员工 ${g.employee_id} · ${g.work_date}`, el("div", {}, [
              rawBlock("上班卡原始行", s.in_raw_obj),
              rawBlock("下班卡原始行", s.out_raw_obj),
            ])),
          }, "查看原始行")),
        ]);
      });
      return el("div", { class: "panel" }, [
        el("div", { class: "flex" }, [
          el("h2", { style: "margin:0" }, `员工 ${g.employee_id} · 工作日 ${g.work_date}`),
          el("span", { class: "spacer" }),
          chip(g.status === "open" ? "error" : "success",
            g.status === "open" ? "待处理（该日班次暂不计入汇总）" : "已解决"),
        ]),
        el("p", { class: "hint" },
          "同一员工同一天出现多套互斥记录。解决前这些班次全部排除在工时汇总之外；选择一套保留后重算。"),
        el("div", { class: "table-wrap" }, el("table", {}, [
          el("thead", {}, el("tr", {},
            ["保留", "来源", "上班(本地)", "下班(本地)", "时长", "标记", "原始行"].map((h) => el("th", {}, h)))),
          el("tbody", {}, choices),
        ])),
        g.status === "open"
          ? el("div", { class: "btn-row" }, [
              el("button", {
                class: "btn primary",
                onClick: async () => {
                  const picked = host.querySelector(`input[name=g${g.id}]:checked`);
                  if (!picked) return toast("请先选择要保留的班次", false);
                  const decision = prompt("处理说明（会记入决议）", "人工核对原始行后保留所选班次");
                  if (decision === null) return;
                  try {
                    await api.resolveConflict(g.id, [Number(picked.value)], decision);
                    toast("冲突已解决，相关周期已重算");
                    load();
                  } catch (exc) {
                    toast(exc.message, false);
                  }
                },
              }, "按所选班次解决并重算"),
            ])
          : el("p", { class: "hint" }, `决议：${(g.resolution || {}).decision || "—"}`),
      ]);
    }));
  }

  root.append(
    el("div", { class: "panel" }, [
      el("div", { class: "flex" }, [
        el("h2", { style: "margin:0" }, "同员工同日记录冲突"),
        el("span", { class: "spacer" }),
        el("button", { class: "btn", onClick: () => download(api.exportUrl.conflicts(), "conflicts.csv") },
          "导出冲突清单 CSV"),
        el("button", { class: "btn", onClick: load }, "刷新"),
      ]),
    ]),
    host,
  );
  await load();
}
