/* Magic Import — small shared helpers (no framework beyond Alpine). */
window.MI = {
  async api(method, url, body, opts = {}) {
    const init = { method, headers: {} };
    if (body instanceof FormData) {
      init.body = body;
    } else if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const res = await fetch(url, init);
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      const err = (data && data.error) || { code: "http_error", message: `HTTP ${res.status}` };
      if (!opts.silent) MI.toast(err.message, "error");
      const e = new Error(err.message);
      e.code = err.code;
      e.details = err.details;
      e.status = res.status;
      throw e;
    }
    return data;
  },
  toast(message, kind = "info", ms = 3800) {
    const host = document.getElementById("toasts");
    if (!host) return;
    const el = document.createElement("div");
    const tone = {
      info: "border-slate-200 bg-white text-slate-800",
      success: "border-emerald-200 bg-emerald-50 text-emerald-900",
      error: "border-rose-200 bg-rose-50 text-rose-900",
    }[kind] || "border-slate-200 bg-white";
    el.className = `pointer-events-auto rounded-xl border px-4 py-3 text-sm shadow-lg transition ${tone}`;
    el.textContent = message;
    host.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 300);
    }, ms);
  },
  fmt(n) {
    return (n ?? 0).toLocaleString("en-US");
  },
  bytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(2)} MB`;
  },
  confBadge(c) {
    return {
      HIGH: "badge badge-high",
      MEDIUM: "badge badge-medium",
      LOW: "badge badge-low",
      UNMAPPED: "badge badge-muted",
    }[c] || "badge badge-muted";
  },
  statusBadge(s) {
    return {
      ready: "badge badge-high",
      warning: "badge badge-medium",
      error: "badge badge-error",
      completed: "badge badge-high",
      validated: "badge badge-info",
      mapped: "badge badge-info",
      mapping: "badge badge-medium",
      uploaded: "badge badge-muted",
    }[s] || "badge badge-muted";
  },
  statusLabel(s) {
    return {
      completed: "Completed",
      validated: "Needs Review",
      mapped: "Ready to validate",
      mapping: "Mapping",
      uploaded: "Uploaded",
    }[s] || s;
  },
  download(url) {
    const a = document.createElement("a");
    a.href = url;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  },
};
