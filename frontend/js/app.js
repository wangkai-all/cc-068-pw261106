// 应用入口与哈希路由。
import { clear, el, toast } from "./dom.js";
import { renderImport } from "./views/import.js";
import { renderBatches } from "./views/batches.js";
import { renderSummary } from "./views/summary.js";
import { renderIssues } from "./views/issues.js";
import { renderConflicts } from "./views/conflicts.js";
import { renderSources } from "./views/sources.js";
import { renderTrace } from "./views/trace.js";

const VIEWS = {
  import: renderImport,
  batches: renderBatches,
  summary: renderSummary,
  issues: renderIssues,
  conflicts: renderConflicts,
  sources: renderSources,
  trace: renderTrace,
};

const view = document.getElementById("view");
const tabs = document.getElementById("tabs");

async function route() {
  const name = (location.hash.replace("#", "") || "import").split("?")[0];
  const renderer = VIEWS[name] || renderImport;
  tabs.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.view === name)
  );
  clear(view);
  view.append(el("div", { class: "hint" }, "加载中…"));
  try {
    clear(view);
    await renderer(view);
  } catch (exc) {
    clear(view);
    view.append(el("div", { class: "panel" }, [
      el("h2", {}, "页面加载失败"),
      el("p", { class: "chip error" }, exc.message),
    ]));
    toast(exc.message, false);
  }
}

tabs.addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (tab) location.hash = tab.dataset.view;
});

window.addEventListener("hashchange", route);
if (!location.hash) location.hash = "import";
route();
