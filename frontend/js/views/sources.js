// 来源与映射：来源格式/时区/字段名映射、员工编号别名、全局计算规则与映射重算。
import { api } from "../api.js";
import { chip, clear, el, toast } from "../dom.js";

const TZ_SAMPLES = ["Asia/Shanghai", "Asia/Tokyo", "America/New_York",
  "America/Los_Angeles", "Europe/London", "Europe/Berlin", "Australia/Sydney", "UTC"];

export async function renderSources(root) {
  const [{ sources }, { aliases }, { settings }] =
    await Promise.all([api.listSources(), api.listAliases(), api.settings()]);

  // ---------- 来源编辑表单 ----------
  const code = el("input", { placeholder: "如 SYS-A，唯一编码" });
  const name = el("input", { placeholder: "显示名称，如 打卡系统A" });
  const fmt = el("select", {}, [
    ["csv", "CSV（逗号分隔）"], ["tsv", "TSV（制表符）"],
    ["xlsx", "XLSX（Excel）"], ["json", "JSON"],
  ].map(([v, t]) => el("option", { value: v }, t)));
  const tz = el("input", { list: "tz-list", placeholder: "IANA 时区，如 America/New_York" });
  const tzList = el("datalist", { id: "tz-list" }, TZ_SAMPLES.map((t) => el("option", { value: t })));
  const encoding = el("select", {}, [
    ["utf-8-sig", "UTF-8（带 BOM 兼容）"], ["utf-8", "UTF-8"], ["gbk", "GBK / GB2312"],
  ].map(([v, t]) => el("option", { value: v }, t)));
  const headerRow = el("input", { type: "number", min: "1", value: "1" });
  const fEmp = el("input", { placeholder: "来源列名，如 工号 / EmpID" });
  const fName = el("input", { placeholder: "来源列名，如 姓名（可空）" });
  const fTime = el("input", { placeholder: "单列时间，如 打卡时间 / Clock" });
  const fDate = el("input", { placeholder: "日期列（与时刻列配套，可空）" });
  const fClock = el("input", { placeholder: "时刻列（与日期列配套，可空）" });
  const fType = el("input", { placeholder: "打卡类型列，如 状态 / Type（可空，靠顺序推断）" });
  const inAliases = el("input", { value: "上班,IN,签到,check-in,CheckIn" });
  const outAliases = el("input", { value: "下班,OUT,签退,check-out,CheckOut" });

  function fillForm(s) {
    code.value = s.code; name.value = s.name; fmt.value = s.format;
    tz.value = s.time_zone; encoding.value = s.encoding;
    headerRow.value = s.options?.header_row || 1;
    const f = s.mapping?.fields || {};
    fEmp.value = f.employee_code || ""; fName.value = f.employee_name || "";
    fTime.value = f.punch_time || ""; fDate.value = f.punch_date || "";
    fClock.value = f.punch_clock || ""; fType.value = f.punch_type || "";
    const pa = s.mapping?.punch_type_aliases || {};
    inAliases.value = (pa.in || []).join(",");
    outAliases.value = (pa.out || []).join(",");
  }

  const sourceRows = sources.map((s) => el("tr", {}, [
    el("td", { class: "mono" }, s.code),
    el("td", {}, s.name),
    el("td", {}, chip("neutral", s.format.toUpperCase())),
    el("td", { class: "small mono" }, s.time_zone),
    el("td", { class: "small" }, Object.values(s.mapping?.fields || {}).filter(Boolean).join("、")),
    el("td", {}, el("button", { class: "btn small", onClick: () => fillForm(s) }, "载入到表单编辑")),
    el("td", {}, el("button", {
      class: "btn small",
      onClick: async () => {
        if (!confirm(`按当前映射修订重算来源 ${s.code} 的全部受影响周期？`)) return;
        try {
          const r = await api.recompute(s.id);
          toast(`已重算：${r.shifts_built} 个班次，涉及 ${r.affected_employees.length} 名员工`);
        } catch (exc) { toast(exc.message, false); }
      },
    }, "修订后重算")),
  ]));

  // ---------- 别名表单 ----------
  const aliasSource = el("select", {}, sources.map((s) =>
    el("option", { value: s.id }, `${s.code} · ${s.name}`)));
  const aliasCode = el("input", { placeholder: "来源中的员工编号，如 X1" });
  const aliasEmp = el("input", { placeholder: "统一内部员工号，如 E002" });
  const aliasName = el("input", { placeholder: "员工姓名（可选）" });

  const aliasRows = aliases.map((a) => el("tr", {}, [
    el("td", { class: "mono small" }, a.source_emp_code),
    el("td", { class: "mono" }, a.employee_id),
    el("td", {}, a.employee_name || "—"),
    el("td", { class: "small" }, sources.find((s) => s.id === a.source_id)?.code || a.source_id),
  ]));

  // ---------- 全局规则 ----------
  const periodType = el("select", {}, [
    ["monthly", "自然月"], ["semimonthly", "半月"], ["weekly", "按周"],
  ].map(([v, t]) => el("option", { value: v, selected: v === settings.period_type }, t)));
  const dailyRegular = el("input", { type: "number", value: settings.daily_regular_minutes });
  const longShift = el("input", { type: "number", value: settings.long_shift_minutes });
  const foldPolicy = el("select", {}, [
    ["first", "first — 取第一次（夏令时实例，默认）"],
    ["second", "second — 取第二次（冬令时实例）"],
  ].map(([v, t]) => el("option", { value: v, selected: v === settings.fold_policy }, t)));

  root.append(
    el("div", { class: "panel" }, [
      el("h2", {}, "来源配置与字段映射"),
      sources.length ? el("div", { class: "table-wrap", style: "max-height:260px;margin-bottom:16px" },
        el("table", {}, [
          el("thead", {}, el("tr", {},
            ["编码", "名称", "格式", "时区", "已映射字段", "编辑", "重算"].map((h) => el("th", {}, h)))),
          el("tbody", {}, sourceRows),
        ])) : el("div", { class: "empty" }, "尚无来源，请在下方创建。"),
      el("div", { class: "grid cols-3" }, [
        el("label", { class: "field" }, [el("span", {}, "来源编码 *"), code]),
        el("label", { class: "field" }, [el("span", {}, "显示名称 *"), name]),
        el("label", { class: "field" }, [el("span", {}, "文件格式"), fmt]),
        el("label", { class: "field" }, [el("span", {}, "来源时区（IANA）*"), tz, tzList]),
        el("label", { class: "field" }, [el("span", {}, "文件编码"), encoding]),
        el("label", { class: "field" }, [el("span", {}, "表头所在行（1 起）"), headerRow]),
      ]),
      el("h3", {}, "字段名映射（填来源文件里的真实列名）"),
      el("div", { class: "grid cols-3" }, [
        el("label", { class: "field" }, [el("span", {}, "员工编号列 *"), fEmp]),
        el("label", { class: "field" }, [el("span", {}, "员工姓名列"), fName]),
        el("label", { class: "field" }, [el("span", {}, "打卡时间列（单列）"), fTime]),
        el("label", { class: "field" }, [el("span", {}, "日期列（拆分式）"), fDate]),
        el("label", { class: "field" }, [el("span", {}, "时刻列（拆分式）"), fClock]),
        el("label", { class: "field" }, [el("span", {}, "打卡类型列"), fType]),
        el("label", { class: "field" }, [el("span", {}, "上班类型取值（逗号分隔）"), inAliases]),
        el("label", { class: "field" }, [el("span", {}, "下班类型取值（逗号分隔）"), outAliases]),
      ]),
      el("p", { class: "hint" },
        "时间用单列（打卡时间列）或 日期+时刻 两列，二选一；类型列留空时按 in→out 顺序配对。每次保存都会生成映射版本，可随时按来源重算受影响周期。"),
      el("div", { class: "btn-row" }, [
        el("button", { class: "btn primary", onClick: async () => {
          if (!code.value || !name.value || !tz.value) return toast("编码、名称、时区为必填", false);
          try {
            await api.saveSource({
              code: code.value.trim(), name: name.value.trim(), format: fmt.value,
              time_zone: tz.value.trim(), encoding: encoding.value,
              options: { header_row: Number(headerRow.value) || 1 },
              mapping: {
                fields: {
                  employee_code: fEmp.value.trim(), employee_name: fName.value.trim() || null,
                  punch_time: fTime.value.trim() || null, punch_date: fDate.value.trim() || null,
                  punch_clock: fClock.value.trim() || null, punch_type: fType.value.trim() || null,
                },
                punch_type_aliases: {
                  in: inAliases.value.split(",").map((x) => x.trim()).filter(Boolean),
                  out: outAliases.value.split(",").map((x) => x.trim()).filter(Boolean),
                },
              },
            });
            toast("来源与映射已保存（已记录映射版本）");
            clear(root); renderSources(root);
          } catch (exc) { toast(exc.message, false); }
        }}, "保存来源与映射"),
      ]),
    ]),

    el("div", { class: "panel" }, [
      el("h2", {}, "员工编号别名（各来源编号 → 统一内部员工号）"),
      el("div", { class: "grid cols-3" }, [
        el("label", { class: "field" }, [el("span", {}, "来源"), aliasSource]),
        el("label", { class: "field" }, [el("span", {}, "来源员工编号"), aliasCode]),
        el("label", { class: "field" }, [el("span", {}, "内部员工号"), aliasEmp]),
        el("label", { class: "field" }, [el("span", {}, "姓名（可选）"), aliasName]),
      ]),
      el("div", { class: "btn-row" }, [
        el("button", { class: "btn primary", onClick: async () => {
          if (!aliasCode.value || !aliasEmp.value) return toast("来源编号与内部员工号必填", false);
          try {
            await api.saveAlias({
              source_id: Number(aliasSource.value),
              source_emp_code: aliasCode.value.trim(),
              employee_id: aliasEmp.value.trim(),
              employee_name: aliasName.value.trim() || null,
            });
            toast("别名已保存"); clear(root); renderSources(root);
          } catch (exc) { toast(exc.message, false); }
        }}, "保存别名"),
      ]),
      aliases.length ? el("div", { class: "table-wrap", style: "max-height:240px;margin-top:14px" },
        el("table", {}, [
          el("thead", {}, el("tr", {},
            ["来源编号", "内部员工号", "姓名", "来源"].map((h) => el("th", {}, h)))),
          el("tbody", {}, aliasRows),
        ])) : el("div", { class: "empty" }, "尚无别名。"),
    ]),

    el("div", { class: "panel" }, [
      el("h2", {}, "全局计算规则"),
      el("div", { class: "grid cols-3" }, [
        el("label", { class: "field" }, [el("span", {}, "薪资周期"), periodType]),
        el("label", { class: "field" }, [el("span", {}, "每日正常工时上限（分钟）"), dailyRegular]),
        el("label", { class: "field" }, [el("span", {}, "超长班次阈值（分钟）"), longShift]),
        el("label", { class: "field" }, [el("span", {}, "DST 回拨模糊时间规则"), foldPolicy]),
      ]),
      el("p", { class: "hint" },
        "时长按 UTC 实际流逝计算：春季跳变日跨夜会少 1 小时、秋季回拨夜会多 1 小时；模糊时间按 fold 规则解释并标记。"),
      el("div", { class: "btn-row" }, [
        el("button", { class: "btn primary", onClick: async () => {
          try {
            await api.saveSettings({
              period_type: periodType.value,
              daily_regular_minutes: dailyRegular.value,
              long_shift_minutes: longShift.value,
              fold_policy: foldPolicy.value,
            });
            const r = await api.recompute(null);
            toast(`规则已保存并全量重算：${r.shifts_built} 个班次`);
          } catch (exc) { toast(exc.message, false); }
        }}, "保存规则并全量重算"),
      ]),
    ]),
  );
}
