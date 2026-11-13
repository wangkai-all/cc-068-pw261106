// 轻量 DOM 辅助与全局提示/弹窗。

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null) continue;
    if (key === "class") node.className = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  const list = Array.isArray(children) ? children : [children];
  list.filter((c) => c != null && c !== false).forEach((c) => {
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  });
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

let toastTimer;
export function toast(message, ok = true) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.className = `toast ${ok ? "ok" : "err"}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add("hidden"), 3600);
}

export function modal(title, contentNode, footerNode) {
  const root = document.getElementById("modal-root");
  const close = () => clear(root);
  const box = el("div", { class: "modal" }, [
    el("div", { class: "flex" }, [
      el("h3", {}, title),
      el("span", { class: "spacer" }),
      el("button", { class: "btn close", onClick: close }, "关闭"),
    ]),
    contentNode,
    footerNode ? el("div", { class: "btn-row right" }, footerNode) : null,
  ]);
  const mask = el("div", { class: "modal-mask", onClick: (e) => {
    if (e.target === mask) close();
  }}, [box]);
  clear(root);
  root.append(mask);
  return { close };
}

export function chip(kind, text) {
  return el("span", { class: `chip ${kind}` }, text);
}

export function fmtHours(minutes) {
  if (minutes == null) return "—";
  return (minutes / 60).toFixed(2);
}

export function download(href, filename) {
  const a = el("a", { href, download: filename || "" });
  document.body.append(a);
  a.click();
  a.remove();
}

const KIND_LABELS = {
  missing_punch: ["error", "缺卡"],
  nonexistent_time: ["warning", "DST缺口时间"],
  ambiguous_time: ["warning", "DST模糊时间"],
  unpaired_punch: ["warning", "未配对"],
  long_shift: ["warning", "超长班次"],
  conflict: ["error", "记录冲突"],
  duplicate_row: ["info", "重复行"],
};

export function issueKindChip(kind) {
  const [cls, label] = KIND_LABELS[kind] || ["neutral", kind];
  return chip(cls, label);
}
