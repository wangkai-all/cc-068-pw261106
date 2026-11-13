// 缺卡与问题：列出 open/resolved 问题，支持对缺卡做手工补卡后自动重算。
import { api } from "../api.js";
import { chip, clear, el, issueKindChip, modal, toast } from "../dom.js";

export async function renderIssues(root) {
  const { sources } = await api.listSources();
  const filter = { status: "open", kind: "", employee_id: "" };
  const host = el("div");

  const statusSel = el("select", {
    onChange: (e) => { filter.status = e.target.value; load(); },
  }, [["open", "待处理"], ["resolved", "已解决"], ["ignored", "已忽略"], ["", "全部"]]
    .map(([v, t]) => el("option", { value: v, selected: v === filter.status }, t)));
  const kindSel = el("select", {
    onChange: (e) => { filter.kind = e.target.value; load(); },
  }, [["", "全部类型"], ["missing_punch", "缺卡"], ["unpaired_punch", "未配对"],
      ["long_shift", "超长班次"], ["conflict", "冲突"], ["nonexistent_time", "DST 缺口"],
      ["ambiguous_time", "DST 模糊"]]
    .map(([v, t]) => el("option", { value: v, selected: v === filter.kind }, t)));
  const empInput = el("input", { placeholder: "按员工编号过滤" });
  empInput.addEventListener("change", () => { filter.employee_id = empInput.value.trim(); load(); });

  async function load() {
    clear(host);
    const { issues } = await api.issues(filter);
    if (!issues.length) {
      host.append(el("div", { class: "empty" }, "没有符合条件的问题。"));
      return;
    }
    host.append(el("div", { class: "table-wrap" }, el("table", {}, [
      el("thead", {}, el("tr", {},
        ["类型", "级别", "员工", "工作日", "来源", "说明", "状态", "原始行", "操作"].map((h) => el("th", {}, h)))),
      el("tbody", {}, issues.map((it) => el("tr", {}, [
        el("td", {}, issueKindChip(it.kind)),
        el("td", {}, chip(it.severity === "error" ? "error" : "warning",
          it.severity === "error" ? "错误" : "警告")),
        el("td", { class: "mono" }, it.employee_id || "—"),
        el("td", { class: "mono small" }, it.work_date || "—"),
        el("td", { class: "small" }, it.source_code || "—"),
        el("td", { class: "small" }, it.message),
        el("td", {}, chip(it.status === "open" ? "error" : "success",
          it.status === "open" ? "待处理" : "已解决")),
        el("td", {}, el("button", {
          class: "btn small",
          onClick: () => modal("问题涉及的原始行",
            el("pre", { class: "raw" }, JSON.stringify(it.raw_refs, null, 2))),
        }, "查看")),
        el("td", {}, it.kind === "missing_punch" && it.status === "open"
          ? el("button", { class: "btn primary small", onClick: () => openManual(it) }, "手工补卡")
          : "—"),
      ]))),
    ])));
  }

  function openManual(issue) {
    const sourceId = issue.source_id || sources[0]?.id;
    const sourceSel = el("select", {}, sources.map((s) =>
      el("option", { value: s.id, selected: s.id === sourceId }, `${s.code} · ${s.name}（${s.time_zone}）`)));
    const typeSel = el("select", {}, [
      el("option", { value: "out" }, "下班卡（补缺下班）"),
      el("option", { value: "in" }, "上班卡（补缺上班）"),
    ]);
    const timeInput = el("input", { type: "text", placeholder: "2026-03-09 18:00（来源时区本地时间）" });
    const reasonInput = el("input", { placeholder: "补卡原因，如：员工补卡申请单#12" });
    const m = modal("手工补卡", el("div", {}, [
      el("p", { class: "hint" }, `员工 ${issue.employee_id}：${issue.message}`),
      el("label", { class: "field" }, [el("span", {}, "归属来源（决定时区）"), sourceSel]),
      el("label", { class: "field" }, [el("span", {}, "补卡类型"), typeSel]),
      el("label", { class: "field" }, [el("span", {}, "本地打卡时间"), timeInput]),
      el("label", { class: "field" }, [el("span", {}, "原因"), reasonInput]),
      el("p", { class: "hint" },
        "补卡写入后仅重算该员工班次；手工补卡会在后续映射重算中保留，并在员工追溯中标记。"),
    ]), [
      el("button", { class: "btn", onClick: () => m.close() }, "取消"),
      el("button", { class: "btn primary", onClick: async () => {
        try {
          await api.manualPunch({
            employee_id: issue.employee_id,
            source_id: Number(sourceSel.value),
            punch_type: typeSel.value,
            local_time: timeInput.value.trim(),
            reason: reasonInput.value.trim() || "手工补卡",
          });
          toast("补卡已写入，员工班次已重算");
          m.close();
          load();
        } catch (exc) {
          toast(exc.message, false);
        }
      }}, "确认补卡并重算"),
    ]);
  }

  root.append(
    el("div", { class: "panel" }, [
      el("h2", {}, "筛选"),
      el("div", { class: "grid cols-3" }, [
        el("label", { class: "field" }, [el("span", {}, "状态"), statusSel]),
        el("label", { class: "field" }, [el("span", {}, "类型"), kindSel]),
        el("label", { class: "field" }, [el("span", {}, "员工"), empInput]),
      ]),
      el("div", { class: "btn-row" }, [el("button", { class: "btn", onClick: load }, "刷新")]),
      el("p", { class: "hint" },
        "缺卡的班次 total_minutes 为空，绝不按零工时计入；补卡前只登记问题，补卡并重算后才产生工时。"),
    ]),
    el("div", { class: "panel" }, [el("h2", {}, "问题清单"), host]),
  );
  await load();
}
