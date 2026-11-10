/* Shared helpers: API calls through the Flask proxy, toasts, escaping, formatting. */
(function () {
  const csrf = document.querySelector('meta[name="csrf"]')?.content || "";

  async function api(method, path, body, params, headers) {
    const qs = params ? "?" + new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null)) : "";
    const opts = { method, credentials: "same-origin", headers: { "X-CSRF-Token": csrf, Accept: "application/json" } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    if (headers) Object.assign(opts.headers, headers);
    let res, data;
    try {
      res = await fetch("/api/" + path.replace(/^\//, "") + qs, opts);
    } catch (e) {
      return { ok: false, status: 0, data: { message: "You appear to be offline. Check your connection and try again." } };
    }
    try { data = await res.json(); } catch (e) { data = {}; }
    if (res.status === 401 && data.sessionExpired) {
      location.href = "/login?expired=1";
    }
    return { ok: res.ok, status: res.status, data };
  }

  function toast(message, type) {
    const box = document.getElementById("toasts");
    if (!box) return;
    const t = document.createElement("div");
    t.className = "toast" + (type === "error" ? " error" : "");
    t.textContent = message;
    box.appendChild(t);
    setTimeout(() => t.remove(), type === "error" ? 6500 : 4000);
  }

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  function initials(name) {
    return (String(name || "?").replace(/^Dr\.?\s+/i, "").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("") || "?").toUpperCase();
  }

  function errorMessage(res, fallback) {
    const d = res.data || {};
    if (d.fields?.length) return `${d.message} (${d.fields.join(", ")})`;
    if (d.missingFields?.length) return `${d.message} (${d.missingFields.join(", ")})`;
    return d.message || fallback || "Something went wrong. Please try again.";
  }

  // "9:00:00" or "09:00:00" -> "09:00"
  function hhmm(t) {
    const m = String(t || "").match(/(\d{1,2}):(\d{2})/);
    return m ? m[1].padStart(2, "0") + ":" + m[2] : "";
  }
  function time12(t) {
    const m = hhmm(t);
    if (!m) return "";
    let [h, mi] = m.split(":").map(Number);
    const ap = h >= 12 ? "pm" : "am";
    h = h % 12 || 12;
    return `${h}:${String(mi).padStart(2, "0")} ${ap}`;
  }
  const money = (v) => (v == null || v === "" ? "—" : "₹" + Number(v).toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: 2 }));
  const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

  function isoDateIn(tz, date = new Date()) {
    try {
      return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
    } catch (e) {
      return new Intl.DateTimeFormat("en-CA").format(date);
    }
  }
  function addDays(iso, n) {
    const d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  }
  function prettyDate(iso, opts) {
    return new Date(iso + "T00:00:00Z").toLocaleDateString("en-GB", Object.assign({ weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC" }, opts));
  }

  /* Busy state for buttons */
  async function withBusy(btn, fn) {
    if (!btn) return fn();
    const label = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> ' + (btn.dataset.busy || "Working…");
    try { return await fn(); } finally { btn.disabled = false; btn.innerHTML = label; }
  }

  /* Tag inputs: <div class="tags" data-tags="name"><input></div> */
  function tagInput(root, initial) {
    const input = root.querySelector("input");
    let values = [];
    function draw() {
      root.querySelectorAll(".tag").forEach((n) => n.remove());
      values.forEach((v, i) => {
        const t = document.createElement("span");
        t.className = "tag";
        t.innerHTML = `${esc(v)}<button type="button" aria-label="Remove ${esc(v)}">×</button>`;
        t.querySelector("button").onclick = () => { values.splice(i, 1); draw(); };
        root.insertBefore(t, input);
      });
    }
    function add(raw) {
      raw.split(",").map((s) => s.trim()).filter(Boolean).forEach((v) => {
        if (!values.some((x) => x.toLowerCase() === v.toLowerCase())) values.push(v);
      });
      input.value = ""; draw();
    }
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(input.value); }
      else if (e.key === "Backspace" && !input.value && values.length) { values.pop(); draw(); }
    });
    input.addEventListener("blur", () => input.value.trim() && add(input.value));
    input.addEventListener("input", () => { if (input.value.includes(",")) add(input.value); });
    root.addEventListener("click", () => input.focus());
    if (initial) { values = [...initial]; draw(); }
    return { get: () => { if (input.value.trim()) add(input.value); return [...values]; }, set: (v) => { values = [...v]; draw(); } };
  }

  function timezones() {
    try { return Intl.supportedValuesOf("timeZone"); } catch (e) {
      return ["Asia/Kolkata", "UTC", "Europe/London", "America/New_York", "America/Los_Angeles", "Asia/Dubai", "Asia/Singapore", "Australia/Sydney"];
    }
  }
  function fillTimezones(datalist) {
    const list = timezones();
    if (!list.includes("UTC")) list.push("UTC");
    datalist.innerHTML = list.map((z) => `<option value="${esc(z)}"></option>`).join("");
  }

  function confirmDialog(title, message, confirmLabel) {
    return new Promise((resolve) => {
      const dlg = document.createElement("dialog");
      dlg.innerHTML = `<div class="dlg"><div class="dlg-head"><h2>${esc(title)}</h2><p>${esc(message)}</p></div>
        <div class="dlg-foot"><button class="btn btn-ghost" value="no">Keep it</button><button class="btn btn-danger" value="yes">${esc(confirmLabel || "Delete")}</button></div></div>`;
      document.body.appendChild(dlg);
      dlg.addEventListener("click", (e) => { const v = e.target.closest("button")?.value; if (v) { dlg.close(); resolve(v === "yes"); } });
      dlg.addEventListener("close", () => { dlg.remove(); resolve(false); });
      dlg.showModal();
    });
  }

  /* Mobile sidebar */
  document.addEventListener("click", (e) => {
    const shell = document.querySelector(".shell");
    if (!shell) return;
    if (e.target.closest(".menu-btn")) shell.classList.toggle("open");
    else if (e.target.closest(".scrim")) shell.classList.remove("open");
  });

  window.MS = { api, toast, esc, $, $$, initials, errorMessage, hhmm, time12, money, DAYS, isoDateIn, addDays, prettyDate, withBusy, tagInput, fillTimezones, confirmDialog };
})();
