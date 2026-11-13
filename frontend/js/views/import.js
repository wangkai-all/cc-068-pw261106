// 导入与预览：选来源 -> 上传 -> 预览转换结果 -> 确认后才写入本地批次。
import { api } from "../api.js";
import { chip, clear, el, fmtHours, modal, toast } from "../dom.js";

export async function renderImport(root) {
  const sources = (await api.listSources()).sources;
  let selectedSource = sources[0]?.id ?? "";
  let previewData = null;
  let pickedFile = null;

  const sourceSelect = el("select", {
    onChange: (e) => { selectedSource = e.target.value; },
  }, sources.length
    ? sources.map((s) => el("option", { value: s.id }, `${s.code} · ${s.name}（${s.format.toUpperCase()} / ${s.time_zone}）`))
    : [el("option", { value: "" }, "请先在「来源与映射」中创建来源")]);

  const fileInput = el("input", { type: "file", style: "display:none" });
  const fileLabel = el("div", { class: "hint" }, "尚未选择文件");
  const dropzone = el("div", { class: "dropzone" }, [
    el("div", { style: "font-size:26px" }, "📄"),
    el("div", { style: "margin-top:6px;font-weight:600;color:var(--text)" }, "点击选择或拖入打卡文件"),
    el("div", { class: "hint" }, "支持 CSV / TSV / XLSX / JSON；文件仅在本地解析，不会上传到外部系统"),
    fileInput,
  ]);

  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) {
      pickedFile = fileInput.files[0];
      fileLabel.textContent = `已选择：${pickedFile.name}（${pickedFile.size.toLocaleString()} 字节）`;
    }
  });
  dropzone.addEventListener("click", () => fileInput.click());
  ["dragover", "dragenter"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove("drag"); }));
  dropzone.addEventListener("drop", (e) => {
    pickedFile = e.dataTransfer.files[0];
    if (pickedFile) {
      fileLabel.textContent = `已选择：${pickedFile.name}（${pickedFile.size.toLocaleString()} 字节）`;
    }
  });

  const noteInput = el("input", { placeholder: "批次备注（可选，如：11月第一周）" });
  const previewPanel = el("div");
  const btnPreview = el("button", {
    class: "btn primary",
    onClick: async () => {
      if (!selectedSource) return toast("请先创建并选择来源", false);
      if (!pickedFile) return toast("请先选择文件", false);
      btnPreview.disabled = true;
      try {
        previewData = await api.preview(selectedSource, pickedFile);
        renderPreview();
      } catch (exc) {
        toast(exc.message, false);
      } finally {
        btnPreview.disabled = false;
      }
    },
  }, "① 预览转换结果");

  const btnCommit = el("button", {
    class: "btn primary",
    onClick: async () => {
      if (!previewData || !pickedFile) return toast("请先预览", false);
      if (previewData.duplicate_file) return toast("该文件已导入过（文件级幂等），禁止重复计时", false);
      btnCommit.disabled = true;
      try {
        const result = await api.commit(selectedSource, pickedFile, noteInput.value);
        toast(`批次 ${result.code} 已写入：${result.written} 行，跳过重复 ${result.skipped_duplicates} 行`);
        previewData = null;
        pickedFile = null;
        fileInput.value = "";
        fileLabel.textContent = "尚未选择文件";
        clear(previewPanel);
      } catch (exc) {
        toast(exc.message, false);
      } finally {
        btnCommit.disabled = false;
      }
    },
  }, "② 确认写入本地批次");

  function renderPreview() {
    clear(previewPanel);
    const d = previewData;
    const stat = (num, lbl, cls = "") => el("div", { class: "stat" }, [
      el("div", { class: `num ${cls}` }, String(num)), el("div", { class: "lbl" }, lbl),
    ]);
    const header = el("div", { class: "panel" }, [
      el("h2", {}, `预览：${d.file_name}`),
      el("dl", { class: "kv" }, [
        el("dt", {}, "来源 / 时区"), el("dd", {}, `${d.source.code} · ${d.source.time_zone}`),
        el("dt", {}, "内容哈希"), el("dd", { class: "mono small" }, d.content_hash.slice(0, 16) + "…"),
        el("dt", {}, "识别列"), el("dd", { class: "small" }, d.columns.join("、") || "（无）"),
      ]),
      el("div", { class: "stat-row", style: "margin-top:14px" }, [
        stat(d.total_rows, "数据行"),
        stat(d.error_count, "错误行", d.error_count ? "chip error" : ""),
        stat(d.duplicate_rows, "重复行（将跳过）", d.duplicate_rows ? "chip warning" : ""),
        stat(d.new_employee_codes.length, "未映射员工编号", d.new_employee_codes.length ? "chip warning" : ""),
      ]),
      d.duplicate_file
        ? el("p", {}, chip("error", `文件级重复：已存在于批次来源文件 ${d.duplicate_file.file_name}，确认提交将被拒绝`))
        : el("p", { class: "hint" }, "文件级幂等：相同内容文件只能成功提交一次"),
      d.new_employee_codes.length
        ? el("div", { class: "panel", style: "box-shadow:none;background:#fffbeb;border-color:#fde68a" }, [
            el("h3", {}, "未映射的员工编号（将临时按来源编号作为内部员工号，建议先到「来源与映射」配置别名）"),
            el("div", { class: "flex" }, d.new_employee_codes.map((c) =>
              el("span", { class: "chip warning" }, `${c.source_emp_code}${c.employee_name ? ` · ${c.employee_name}` : ""}`))),
          ])
        : null,
    ]);

    const rows = d.rows.map((r) => el("tr", {}, [
      el("td", { class: "mono small" }, String(r.line_no)),
      el("td", {}, r.source_emp_code || "—"),
      el("td", {}, r.mapped_employee_id
        ? chip("success", r.mapped_employee_id)
        : chip("warning", "未映射")),
      el("td", {}, r.punch_type ? chip("neutral", r.punch_type) : chip("muted", "推断")),
      el("td", { class: "mono small" }, r.local_time || chip("error", "缺卡")),
      el("td", {}, r.dst_kind === "ok" ? "—" : chip("warning", r.dst_kind)),
      el("td", {}, r.duplicate ? chip("info", "重复跳过") : "—"),
      el("td", {}, [
        ...r.errors.map((x) => el("div", {}, chip("error", x))),
        ...r.warnings.map((x) => el("div", {}, chip("warning", x))),
      ]),
      el("td", {}, el("button", {
        class: "btn small",
        onClick: () => modal(`原始行 #${r.line_no}`, el("pre", { class: "raw" }, JSON.stringify(r.raw, null, 2))),
      }, "查看原始行")),
    ]));

    const table = el("div", { class: "panel" }, [
      el("h2", {}, "逐行转换结果（确认前不写入任何数据）"),
      el("div", { class: "table-wrap" }, el("table", {}, [
        el("thead", {}, el("tr", {}, ["行号", "来源员工号", "内部员工号", "打卡类型", "本地时间", "DST", "去重", "问题", "原始行"]
          .map((h) => el("th", {}, h)))),
        el("tbody", {}, rows),
      ])),
    ]);
    previewPanel.append(header, table);
  }

  root.append(
    el("div", { class: "panel" }, [
      el("h2", {}, "第一步：选择数据来源"),
      el("label", { class: "field" }, [el("span", {}, "来源配置"), sourceSelect]),
      el("h2", {}, "第二步：选择打卡文件"),
      dropzone,
      fileLabel,
      el("label", { class: "field", style: "margin-top:14px" }, [el("span", {}, "批次备注"), noteInput]),
      el("div", { class: "btn-row" }, [btnPreview, btnCommit]),
      el("p", { class: "hint" },
        "规则：相同文件重复提交不重复计时；同内容行自动去重；缺少打卡时间按缺卡处理，绝不计为零工时。"),
    ]),
    previewPanel,
  );
}
