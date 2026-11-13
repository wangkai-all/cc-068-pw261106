// 后端 API 封装：全部请求只发往本地服务，不接触任何外部系统。

async function request(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let message = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      message = data.error || message;
    } catch {
      /* 忽略非 JSON 错误体 */
    }
    throw new Error(message);
  }
  return resp.json();
}

function upload(path, fields, file) {
  const form = new FormData();
  Object.entries(fields).forEach(([k, v]) => form.append(k, v));
  form.append("file", file, file.name);
  return fetch(path, { method: "POST", body: form }).then(async (resp) => {
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
  });
}

export const api = {
  health: () => request("GET", "/api/health"),
  listSources: () => request("GET", "/api/sources"),
  saveSource: (payload) => request("POST", "/api/sources", payload),
  listAliases: () => request("GET", "/api/aliases"),
  saveAlias: (payload) => request("POST", "/api/sources/alias", payload),
  preview: (sourceId, file) =>
    upload("/api/preview", { source_id: sourceId }, file),
  commit: (sourceId, file, note) =>
    upload("/api/imports/commit", { source_id: sourceId, note: note || "" }, file),
  recompute: (sourceId) =>
    request("POST", "/api/recompute", sourceId ? { source_id: sourceId } : {}),
  listBatches: () => request("GET", "/api/batches"),
  voidBatch: (batchId) => request("POST", "/api/batches/void", { batch_id: batchId }),
  summary: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== "" && v != null)
    ).toString();
    return request("GET", `/api/summary${qs ? `?${qs}` : ""}`);
  },
  issues: (params = {}) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== "" && v != null)
    ).toString();
    return request("GET", `/api/issues${qs ? `?${qs}` : ""}`);
  },
  conflicts: () => request("GET", "/api/conflicts"),
  resolveConflict: (groupId, keepShiftIds, decision) =>
    request("POST", "/api/conflicts/resolve", {
      group_id: groupId,
      keep_shift_ids: keepShiftIds,
      decision,
    }),
  manualPunch: (payload) => request("POST", "/api/adjustments/manual-punch", payload),
  employees: () => request("GET", "/api/employees"),
  trace: (employeeId) => request("GET", `/api/trace?employee_id=${encodeURIComponent(employeeId)}`),
  settings: () => request("GET", "/api/settings"),
  saveSettings: (payload) => request("POST", "/api/settings", payload),
  exportUrl: {
    reconciliation: (params = {}) =>
      `/api/export/reconciliation?${new URLSearchParams(params).toString()}`,
    trace: (employeeId) => `/api/export/trace?employee_id=${encodeURIComponent(employeeId)}`,
    conflicts: () => "/api/export/conflicts",
  },
};
