/* Model Hub dashboard. Vanilla JS, no build step. All remote text is inserted with
   textContent (via el()), never innerHTML, so model names/descriptions can't inject markup. */
"use strict";

// ---------- helpers ----------
const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "text") node.textContent = v;
    else if (k === "style") node.style.cssText = v; // CSSOM is allowed under our CSP; style="" attributes are not
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: {} };
  if (method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-Model-Hub"] = "1";
    opts.body = JSON.stringify(body || {});
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

const fmt = {
  gb: (v, d = 1) => (v === null || v === undefined || isNaN(v)) ? "–" : `${Number(v).toFixed(d)} GB`,
  b: (bytes, d = 1) => fmt.gb(bytes / 1e9, d),
  params: (p) => !p ? "–" : p >= 1e12 ? `${(p / 1e12).toFixed(2)}T` : p >= 1e9 ? `${(p / 1e9).toFixed(p < 1e10 ? 1 : 0)}B` : `${(p / 1e6).toFixed(0)}M`,
  num: (v) => v === null || v === undefined ? "–" : Number(v).toLocaleString(),
  tps: (v) => v ? `${Math.round(v)} tok/s` : "–",
  secs: (s) => !s ? "–" : s < 90 ? `${Math.round(s)} s` : `${(s / 60).toFixed(1)} min`,
  date: (d) => {
    if (!d) return "–";
    const dt = typeof d === "number" ? new Date(d < 1e12 ? d * 1000 : d) : new Date(d);
    return isNaN(dt) ? "–" : dt.toISOString().slice(0, 10);
  },
  pct: (a, b) => b ? Math.min(100, Math.max(0, (a / b) * 100)) : 0,
  money: (v, cur = "USD") => v === null || v === undefined ? "–" : `${cur === "USD" ? "$" : cur + " "}${v < 0.01 ? v.toFixed(4) : v.toFixed(3)}`,
};

function toast(msg, ms = 4000) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

function confirmDialog(title, bodyNodes) {
  return new Promise((resolve) => {
    const d = $("#dialog");
    $("#dialog-title").textContent = title;
    fill($("#dialog-body"), ...bodyNodes);
    $("#dialog-ok").classList.remove("hidden");
    d.onclose = () => resolve(d.returnValue === "ok");
    d.showModal();
  });
}

function fitPill(fit, fitsNow) {
  if (!fit) return el("span", { class: "pill" }, "unknown");
  const map = {
    "fits in dedicated GPU memory": ["ok", "fits (GPU)"],
    "fits using shared memory": ["info", "fits (borrowed RAM)"],
    "partial CPU offload (slow)": ["warn", "partial CPU offload"],
    "CPU only (slow)": ["warn", "CPU only"],
    "does not fit": ["bad", "does not fit"],
  };
  const [cls, label] = map[fit] || ["", fit];
  return [el("span", { class: `pill ${cls}`, title: fit }, label),
    fitsNow === undefined || fit === "does not fit" ? null
      : el("div", { class: "small muted" }, fitsNow ? "room right now" : "free memory first")];
}

function publisherPill(p, modified = false) {
  if (!p) return null;
  const cls = p === "official" ? "ok" : p === "known quantizer" ? "info" : "warn";
  return [el("span", { class: `pill ${cls}`, title: "Publisher trust tier (config/sources.json)" }, p),
    modified ? el("span", { class: "pill bad", title: "Third-party fine-tune or merge (e.g. uncensored/abliterated/distilled); not the original model" }, "modified") : null];
}

function bar(used, total, extra = 0) {
  const p = fmt.pct(used, total);
  const cls = p > 90 ? "full" : p > 75 ? "hot" : "";
  return el("div", { class: "bar", role: "img", "aria-label": `${p.toFixed(0)}% used` },
    el("span", { class: `used ${cls}`, style: `width:${p}%` }),
    extra ? el("span", { class: "extra", style: `width:${fmt.pct(extra, total)}%` }) : null);
}

function table(headers, rows) {
  return el("div", { class: "table-wrap" },
    el("table", {}, el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, h)))), el("tbody", {}, rows)));
}

// replaceChildren() would render null as the text "null"; drop empty entries first.
function fill(target, ...kids) {
  target.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
}

function errorBox(target, err) {
  target.className = "empty error";
  target.replaceChildren(`Couldn't load: ${err.message}`);
}

function repoLink(id, source = "huggingface") {
  const base = source === "modelscope" ? "https://www.modelscope.cn/models/" : source === "github" ? "https://github.com/" : "https://huggingface.co/";
  return el("a", { href: base + id, target: "_blank", rel: "noopener noreferrer" }, id);
}

function openRepo(id, source = "huggingface") {
  selectTab("search");
  loadRepo(id, source);
}

// ---------- 1. resources ----------
let lastSystem = null;
async function loadResources() {
  try {
    const s = await api("/api/system");
    lastSystem = s;
    renderResources(s);
    $("#updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    fill($("#resources"), el("div", { class: "empty error" }, `System status unavailable: ${e.message}`));
  }
}

function renderResources(s) {
  const g = s.gpu, m = s.memory, d = s.disk, t = s.turnstone;
  const running = Object.entries(s.servers).filter(([, v]) => v.running || v.state === "loading");
  const carved = m.installed_gb && m.installed_gb - m.total_gb > 1 ? m.installed_gb - m.total_gb : 0;
  const gpuDetail = g.kind === "integrated"
    ? `unified memory · ${fmt.gb(g.used_dedicated_gb)} of ${fmt.gb(g.dedicated_gb, 0)} BIOS-reserved + ${fmt.gb(g.used_shared_gb)} of ${fmt.gb(g.shared_limit_gb, 0)} borrowable from Windows RAM in use` +
      (g.plan ? ` · fit uses planned split (${g.plan.gpu_reserved_gb} GB reserved)` : "")
    : g.kind === "discrete" ? `${fmt.gb(g.free_gb)} VRAM free · discrete GPU (models beyond VRAM spill to CPU RAM)` : "no GPU detected: CPU inference";
  const cards = [
    card("CPU", `${s.cpu.percent}%`, bar(s.cpu.percent, 100),
      `${s.cpu.name} · ${s.cpu.cores} threads`),
    card("Memory (Windows)", `${fmt.gb(m.used_gb)} / ${fmt.gb(m.total_gb, 0)}`, bar(m.used_gb, m.total_gb),
      `${fmt.gb(m.available_gb)} available` + (m.installed_gb ? ` · ${m.installed_gb.toFixed(0)} GB installed` : "") +
      (carved ? ` · ${carved.toFixed(0)} GB reserved for the GPU by the BIOS (adjustable; see Hardware detection)` : "")),
    card(`GPU memory${g.name ? " · " + g.name : ""}`, g.kind !== "none" ? `${fmt.gb(g.used_gb)} / ${fmt.gb(g.usable_total_gb, 0)}` : "–",
      g.kind !== "none" ? bar(g.used_gb, g.usable_total_gb) : null, gpuDetail),
    card("Memory for models", `${fmt.gb(g.free_now_gb)} free now`, bar(Math.max(0, g.gpu_capacity_gb - g.free_now_gb), g.gpu_capacity_gb || 1),
      `fit limit ${fmt.gb(g.kind === "discrete" ? g.offload_capacity_gb : g.gpu_capacity_gb)}` +
      (g.kind === "integrated" && !g.plan ? ` = ${fmt.gb(g.dedicated_gb - g.reserve_gb, 0)} reserved + ${fmt.gb(g.gpu_capacity_gb - (g.dedicated_gb - g.reserve_gb), 0)} borrowed` : g.plan ? " (planned split)" :g.kind === "discrete" ? ` (${fmt.gb(g.gpu_capacity_gb, 0)} VRAM + RAM offload)` : "") +
      ` · after ${fmt.gb(g.windows_reserve_gb, 0)} for Windows and ${fmt.gb(g.turnstone_stack_gb, 0)} for Turnstone`),
    card("Disk (models drive)", `${fmt.gb(d.used_gb, 0)} / ${fmt.gb(d.total_gb, 0)}`, bar(d.used_gb, d.total_gb),
      `${fmt.gb(d.available_gb, 0)} free · models use ${fmt.gb(d.models_gb)}`),
    card("Turnstone", t.up ? `up · ${t.nodes} node${t.nodes === 1 ? "" : "s"}` : "not running", null,
      t.up ? `${t.versions?.join(", ") || ""} · up to ${t.node_memory_reserved_gb} GB reserved for nodes` :
        `WSL ${t.wsl_running ? "running" : "stopped"} · start with turnstone-start.ps1`),
    card("Model servers", `${running.length} running`, null,
      running.length ? running.map(([n, v]) => `${n}${v.state === "loading" ? " (loading)" : ""}`).join(", ") : "none loaded"),
  ];
  fill($("#resources"), ...cards);
}

function card(label, value, barNode, detail, extra = null) {
  return el("div", { class: "card" }, el("div", { class: "label" }, label), el("div", { class: "value" }, value),
    barNode, el("div", { class: "detail" }, detail), extra);
}

// ---------- hardware detection panel ----------
async function loadHardware(redetect = false) {
  const body = $("#hw-body");
  try {
    const h = await api(redetect ? "/api/hardware/redetect" : "/api/hardware", redetect ? { method: "POST" } : {});
    const b = h.budget, bw = h.ram_bandwidth;
    const details = $("#hw-details");
    details.classList.toggle("inactive", !h.unified_memory);
    $("#hw-summary").textContent = h.unified_memory
      ? "Hardware detection · unified memory detected (RAM split planner available; RAM figures are indicative)"
      : `Hardware detection · unified memory not detected (${h.kind === "discrete" ? "GPU has its own VRAM" : "no GPU"}) — RAM split planner unavailable`;
    const row = (k, v, src) => [el("dt", {}, k), el("dd", {}, v, src ? el("span", { class: "src" }, src) : null)];
    fill(body,
      h.unified_memory ? unifiedRamNote() : null,
      el("dl", { class: "kv" },
        row("GPU", `${b.name || "none"} (${b.vendor || "?"}, ${b.kind})`, b.sources.kind),
        row(b.kind === "integrated" ? "GPU-reserved by BIOS (carve-out)" : "Dedicated VRAM", fmt.gb(b.dedicated_gb), b.sources.dedicated),
        row(b.kind === "integrated" ? "Windows RAM the GPU may borrow" : "Shared memory limit", b.kind === "integrated" ? fmt.gb(b.shared_limit_gb) : "not used for models", b.sources.shared_limit),
        row("GPU memory in use", `${fmt.gb(b.used_dedicated_gb)} dedicated, ${fmt.gb(b.used_shared_gb)} shared`, b.sources.usage),
        row("RAM installed / visible to Windows", `${fmt.gb(h.installed_ram_gb, 0)} / ${fmt.gb(lastSystem?.memory.total_gb)}`, "detected (Windows)"),
        row("RAM bandwidth (peak)", bw.peak_gbps ? `${bw.peak_gbps.toFixed(0)} GB/s · ${bw.modules} modules × ${bw.mt_s} MT/s, ${bw.bus_bits}-bit` : "unknown", `${bw.source}; ${bw.layout || ""}`),
        row("Bandwidth used for speed estimates", `${h.effective_bandwidth.gbps.toFixed(0)} GB/s`, h.effective_bandwidth.source),
        row("Compute used for prompt estimates", `${h.effective_tflops.value} TFLOPS`, h.effective_tflops.source),
        row("Reserved", `${fmt.gb(b.reserve_gb, 0)} GPU, ${fmt.gb(b.windows_reserve_gb, 0)} Windows, ${fmt.gb(b.turnstone_stack_gb, 0)} Turnstone stack`, "configured (hub.json); Turnstone = nodes × 4 GB + 4 GB"),
        row("Inference backend sees", (h.backend_devices || []).map((d) => `${d.id}: ${d.name} ${fmt.gb(d.total_gb)}`).join(", ") || "llama-server not found", "llama-server --list-devices"),
        row("Other adapters", (h.adapters || []).filter((a) => a.luid !== b.luid).map((a) => `${a.name} (${a.kind})`).join(", ") || "none")),
      el("p", { class: "small" }, "Nothing here is typed in unless hub.json overrides it. ",
        el("button", { type: "button", class: "small ghost", onclick: () => { loadHardware(true); toast("Re-detecting hardware..."); } }, "Re-detect")),
      h.unified_memory ? el("div", { id: "split-box" }, "Loading RAM split...") : splitUnavailable(h));
    if (h.unified_memory) loadSplit();
  } catch (e) { errorBox(body, e); }
}

// Disclaimer shown whenever unified memory is detected (see docs/ASSUMPTIONS.md, "RAM figures on
// unified-memory PCs are indicative").
function unifiedRamNote() {
  return el("div", { class: "hw-note", role: "note" },
    el("strong", {}, "Note: RAM figures are indicative. "),
    "Unified memory was detected, so CPU and GPU share one pool of RAM. Windows counts that pool in " +
    "separate, overlapping buckets (BIOS-reserved, Windows RAM, GPU shared memory), and the GPU driver can " +
    "allocate in ways those counters don't show. RAM used, RAM available, GPU memory and the RAM split " +
    "may therefore not be correct, and “fit” is an estimate. Check Task Manager and benchmark before relying on them.");
}

// Greyed-out placeholder shown when the GPU does not share system RAM.
function splitUnavailable(h) {
  const why = h.kind === "discrete"
    ? "This PC's GPU has its own VRAM, so there is no CPU/GPU RAM split to plan. Models larger than VRAM are estimated as partial CPU offload."
    : "No GPU was detected, so models run on the CPU from system RAM and there is no split to plan.";
  return el("fieldset", { class: "split-disabled", disabled: true, "aria-disabled": "true" },
    el("legend", {}, "RAM split (unified memory) — unavailable"),
    el("p", { class: "small" }, why),
    el("div", { class: "row small" }, "What if the BIOS reserved ", el("input", { type: "number", value: "–", disabled: true }), " GB for the GPU ",
      el("button", { type: "button", class: "small", disabled: true }, "Calculate"),
      el("button", { type: "button", class: "small", disabled: true }, "Use this split for fit estimates")),
    el("p", { class: "small" }, `Detection: ${h.kind_source}. If this is wrong, set hardware.gpu_kind_override in config/hub.json ("integrated", "discrete" or "none") and press Re-detect.`));
}

// ---------- RAM split (unified memory) ----------
async function loadSplit(reserved, fraction) {
  const box = $("#split-box");
  if (!box) return;
  const qs = reserved !== undefined ? `?reserved=${reserved}&fraction=${fraction}` : "";
  try {
    const s = await api(`/api/hardware/split${qs}`);
    const c = s.current, p = s.planned, saved = s.saved_plan || {};
    const seg = (gb, cls, label) => el("span", { class: `seg ${cls}`, style: `flex:${Math.max(gb, 0.01)}`, title: `${label}: ${fmt.gb(gb)}` }, gb >= 6 ? `${label} ${fmt.gb(gb, 0)}` : "");
    const splitBar = (reservedGb, windowsGb, sharedGb) => el("div", { class: "splitbar" },
      seg(reservedGb, "gpu", "GPU-reserved"), seg(sharedGb, "shared", "GPU may borrow"), seg(windowsGb - sharedGb, "cpu", "Windows only"));
    const resInput = el("input", { type: "number", min: 0, max: c.installed_gb - 8, step: 0.5, value: p.gpu_reserved_gb.toFixed(1), "aria-label": "GPU reservation in GB" });
    const fracInput = el("input", { type: "number", min: 10, max: 100, step: 5, value: Math.round(p.shared_fraction * 100), "aria-label": "Shared share percent" });
    const recalc = () => loadSplit(resInput.value, (fracInput.value / 100).toFixed(3));
    const presets = [0.5, 16, 32, 48, 64, 96].map((g) => el("button", { type: "button", class: "small ghost", onclick: () => { resInput.value = g; recalc(); } }, `${g} GB`));
    fill(box,
      el("h3", {}, "RAM split (unified memory)"),
      el("p", { class: "small" }, `The ${fmt.gb(c.installed_gb, 0)} of RAM is one physical pool shared by CPU and GPU (same chips, same speed, no separate VRAM). ` +
        `The BIOS currently reserves ${fmt.gb(c.gpu_reserved_gb)} for the GPU ("UMA Frame Buffer Size"), so Windows sees ${fmt.gb(c.windows_gb)}. ` +
        `Windows then lets the GPU borrow up to ${fmt.gb(c.shared_limit_gb)} (${Math.round(c.shared_fraction * 100)}%) of that as shared memory.`),
      el("div", { class: "small muted" }, "Now:"), splitBar(c.gpu_reserved_gb, c.windows_gb, c.shared_limit_gb),
      el("div", { class: "row small", style: "margin-top:10px" }, "What if the BIOS reserved ", resInput, " GB for the GPU and Windows allowed ", fracInput, "% shared ",
        el("button", { type: "button", class: "small", onclick: recalc }, "Calculate"), ...presets),
      el("div", { class: "small muted" }, "Planned:"), splitBar(p.gpu_reserved_gb, p.windows_gb, p.shared_limit_gb),
      el("dl", { class: "kv" },
        el("dt", {}, "Windows RAM"), el("dd", {}, fmt.gb(p.windows_gb)),
        el("dt", {}, "GPU memory for models"), el("dd", {}, `${fmt.gb(p.gpu_capacity_gb)} (${fmt.gb(p.from_reserved_gb)} reserved + ${fmt.gb(p.from_shared_gb)} borrowed)`),
        el("dt", {}, "Windows left when a model uses all of it"), el("dd", {}, `${fmt.gb(p.windows_left_with_models_gb)} (incl. ${fmt.gb(p.turnstone_stack_gb, 0)} for Turnstone)`),
        el("dt", {}, "Installed models"), el("dd", {}, s.models.map((m) => el("span", { class: `pill ${m.fit.startsWith("fits in") ? "ok" : m.fit.startsWith("fits") ? "warn" : "bad"}`, title: `${fmt.gb(m.need_gb)} needed` }, `${m.name}: ${m.fit.replace("fits ", "")}`)))),
      el("div", { class: "row small" },
        saved.enabled ? el("span", { class: "pill info" }, `Fit estimates use the saved plan: ${saved.gpu_reserved_gb} GB reserved, ${Math.round(saved.shared_fraction * 100)}% shared`)
          : el("span", { class: "pill" }, "Fit estimates use the current BIOS split"),
        el("button", { type: "button", class: "small", onclick: () => savePlan(true, resInput.value, fracInput.value / 100) }, "Use this split for fit estimates"),
        saved.enabled ? el("button", { type: "button", class: "small ghost", onclick: () => savePlan(false) }, "Back to current split") : null),
      el("p", { class: "small muted" }, "Planned figures are indicative: they assume the driver behaves as it does now, which may not hold after a BIOS change. " +
        "This is a planner: Model Hub cannot change the split. To apply one, set UMA Frame Buffer Size in the BIOS (Advanced → AMD CBS / Integrated Graphics), then use Re-detect. " +
        `The shared % after a BIOS change is decided by Windows and the GPU driver (Windows usually allows 50%; this PC currently allows ${Math.round(c.shared_fraction * 100)}%) — check it with Re-detect after changing.`));
  } catch (e) { errorBox(box, e); }
}

async function savePlan(enabled, reserved, fraction) {
  try {
    await api("/api/hardware/plan", { method: "POST", body: enabled ? { enabled, gpu_reserved_gb: Number(reserved), shared_fraction: Number(fraction) } : { enabled: false } });
    toast(enabled ? "Fit estimates now use the planned split" : "Fit estimates use the current BIOS split again");
    loaded.clear(); loadResources(); loadSplit(reserved, fraction);
    const active = document.querySelector(".tabs button.active")?.dataset.tab || "installed";
    selectTab(active);
  } catch (e) { toast(e.message, 8000); }
}

// ---------- monitoring modes ----------
let monTimer = null, monSince = null, monSamples = [];
const monLabels = { continuous: "Continuous", duration: "Set duration", average: "Average over duration", stopped: "Stopped" };

function monSettings() {
  try { return JSON.parse(localStorage.getItem("modelhub.monitor") || "{}"); } catch { return {}; }
}

function syncMonitorForm() {
  const mode = $("#mon-mode").value;
  $("#mon-dur-wrap").classList.toggle("hidden", mode === "continuous" || mode === "stopped");
  $("#mon-start").classList.toggle("hidden", mode === "stopped");
}

async function startMonitor(ev) {
  ev?.preventDefault();
  const mode = $("#mon-mode").value;
  const seconds = Number($("#mon-dur").value) * Number($("#mon-unit").value);
  const interval = Number($("#mon-int").value);
  try { localStorage.setItem("modelhub.monitor", JSON.stringify({ mode, dur: $("#mon-dur").value, unit: $("#mon-unit").value, interval })); } catch { /* ignore */ }
  try {
    const st = await api("/api/monitor", { method: "POST", body: { mode, seconds, interval } });
    monSince = null; monSamples = [];
    renderMonitor(st);
    if (st.running) pollMonitor(); else loadResources();
  } catch (e) { toast(e.message, 8000); }
}

async function stopMonitor() {
  try { renderMonitor(await api("/api/monitor", { method: "POST", body: { mode: "stopped" } })); } catch (e) { toast(e.message); }
  clearTimeout(monTimer);
}

async function pollMonitor() {
  clearTimeout(monTimer);
  try {
    const st = await api(`/api/monitor${monSince ? `?since=${monSince}` : ""}`);
    monSamples = monSamples.concat(st.samples).slice(-600);
    if (st.samples.length) monSince = st.samples[st.samples.length - 1].t;
    renderMonitor(st);
    if (st.running) {
      if (!st.count || st.mode !== "average") loadResources();  // average mode: cards wait for the result
      monTimer = setTimeout(pollMonitor, Math.max(1000, st.interval * 1000));
    } else {
      loadResources();
    }
  } catch (e) { $("#mon-status").textContent = `Monitor unavailable: ${e.message}`; }
}

function spark(metric, max) {
  const pts = monSamples.filter((s) => s[metric] !== null && s[metric] !== undefined);
  if (pts.length < 2) return null;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "spark"); svg.setAttribute("viewBox", "0 0 100 30"); svg.setAttribute("preserveAspectRatio", "none");
  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  line.setAttribute("points", pts.map((s, i) => `${(i / (pts.length - 1)) * 100},${30 - (s[metric] / max) * 28}`).join(" "));
  svg.append(line);
  return svg;
}

function renderMonitor(st) {
  const status = $("#mon-status");
  const left = st.remaining != null ? ` · ${fmt.secs(st.remaining)} left` : "";
  status.textContent = st.running
    ? `${monLabels[st.mode]}: sampling every ${st.interval}s · ${st.count} samples · ${fmt.secs(st.elapsed)} elapsed${left}`
    : st.started ? `Stopped (${st.stop_reason || "stopped"}) after ${fmt.secs(st.elapsed)}, ${st.count} samples. No background sampling.` : "Stopped. No background sampling; use Refresh for a one-off reading.";
  $("#mon-stop").disabled = !st.running;
  const box = $("#mon-stats");
  const s = st.stats || {};
  if (!st.count || !s.cpu_percent) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const avgMode = st.mode === "average";
  const title = avgMode ? (st.running ? "Averaging (in progress)" : "Average over the period") : "Over the monitored period";
  const memTotal = lastSystem?.memory.total_gb || 100, gpuTotal = lastSystem?.gpu.usable_total_gb || 100;
  const statCard = (label, m, unit, max) => card(`${label} · ${title}`,
    `avg ${unit(m.avg)}`, null, `min ${unit(m.min)} · peak ${unit(m.max)} · last ${unit(m.last)}`, spark(label === "CPU" ? "cpu_percent" : label === "RAM" ? "ram_used_gb" : "gpu_used_gb", max));
  fill(box,
    statCard("CPU", s.cpu_percent, (v) => `${v.toFixed(1)}%`, 100),
    statCard("RAM", s.ram_used_gb, (v) => fmt.gb(v), memTotal),
    s.gpu_used_gb ? statCard("GPU memory", s.gpu_used_gb, (v) => fmt.gb(v), gpuTotal) : null);
}

// ---------- installed ----------
async function loadInstalled() {
  const box = $("#installed");
  try {
    const data = await api("/api/local");
    const ts = data.turnstone;
    const rows = data.models.map((m) => {
      const e = m.estimate, srv = m.server || {};
      const state = srv.running ? "running" : srv.state === "loading" ? "loading" : "stopped";
      const measured = m.benchmark?.decode_tokens_per_s;
      // No speed for a model that can't load here: the estimate would assume it fits.
      const tg = measured || (e.fit === "does not fit" ? null : e.decode_tokens_per_s_calibrated || e.decode_tokens_per_s);
      const turn = tg && e.prefill_tokens_per_s ? e.agent_turn.prompt_tokens / e.prefill_tokens_per_s + e.agent_turn.output_tokens / tg : null;
      return el("tr", {},
        el("td", {}, el("div", { class: "name" }, m.name),
          el("div", { class: "small muted" }, m.repo ? repoLink(m.repo) : m.file),
          !m.present ? el("span", { class: "pill bad" }, "file missing") : m.verified ? el("span", { class: "pill ok" }, "sha256 verified") : el("span", { class: "pill warn" }, "not verified")),
        el("td", {}, (m.used_for || []).map((u) => el("span", { class: "pill" }, u))),
        el("td", { class: "num" }, fmt.params(m.params), el("div", { class: "small muted" }, e.active_params && e.active_params !== m.params ? `${fmt.params(e.active_params)} active` : "dense")),
        el("td", { class: "num" }, fmt.gb(m.size_gb)),
        el("td", { class: "num" }, fmt.gb(e.memory_needed_gb), el("div", { class: "small muted" }, `${(e.ctx_total / 1024).toFixed(0)}k ctx`)),
        el("td", {}, fitPill(e.fit, e.fits_now)),
        el("td", { class: "num" }, fmt.tps(tg),
          el("div", { class: "small muted" }, measured ? "measured" : data.calibration.samples ? "calibrated est." : "estimate")),
        el("td", { class: "num" }, fmt.secs(turn)),
        el("td", {}, el("span", { class: `pill ${state === "running" ? "ok" : state === "loading" ? "warn" : ""}` }, state),
          el("div", { class: "small muted" }, `port ${m.port}`)),
        el("td", {}, m.in_turnstone === true ? el("span", { class: "pill ok" }, "registered")
          : m.in_turnstone === false ? el("span", { class: "pill" }, "not registered")
            : el("span", { class: "pill", title: ts.error || "Add a Turnstone API token (docs/INTEGRATION.md)" }, ts.error ? "unreachable" : "no token")),
        el("td", {}, el("div", { class: "actions" },
          state === "stopped" ? el("button", { class: "small", disabled: !m.present, onclick: () => act(m.name, "start") }, "Start")
            : el("button", { class: "small ghost", onclick: () => act(m.name, "stop") }, "Stop"),
          el("button", { class: "small ghost", disabled: state !== "running", onclick: () => act(m.name, "benchmark"), title: "Measure real speed (~30 s)" }, "Benchmark"),
          el("button", { class: "small ghost", disabled: state !== "running", onclick: () => act(m.name, "reach-test"), title: "Check Turnstone's containers can reach this model server" }, "Test reach"),
          el("button", { class: "small ghost", disabled: state !== "running" || !ts.token || m.in_turnstone, onclick: () => act(m.name, "register"), title: "Add to Turnstone's Models tab" }, "Register"))));
    });
    box.className = "";
    fill(box,
      ts.token ? el("div", { id: "ts-endpoints", class: "row small muted" }, "Checking Turnstone endpoints...") : null,
      table(["Model", "Used for", "Params", "Size", "Memory needed", "Fit", "Speed", "Agent turn", "Server", "Turnstone", ""], rows),
      el("p", { class: "muted small" },
        `Agent turn = ${data.models[0]?.estimate.agent_turn.prompt_tokens ?? "?"} prompt + ${data.models[0]?.estimate.agent_turn.output_tokens ?? "?"} output tokens. `,
        data.calibration.samples ? `Estimates calibrated from ${data.calibration.samples} benchmark(s), factor ${data.calibration.factor.toFixed(2)}. ` : "Benchmark a running model to calibrate estimates. ",
        `Models loaded now: ${fmt.gb(data.loaded_gb)}. `,
        "Model files stay in llm\\models on Windows; Turnstone reaches them through each model server at http://<WSL adapter address>:<port>/v1."));
    if (ts.token) checkEndpoints();
  } catch (e) { errorBox(box, e); }
}

async function checkEndpoints() {
  const box = $("#ts-endpoints");
  if (!box) return;
  try {
    const list = await api("/api/turnstone/endpoints");
    const stale = list.filter((x) => !x.ok);
    fill(box, stale.length
      ? [el("span", { class: "pill bad" }, `${stale.length} stale`),
        ` Turnstone points ${stale.map((x) => x.alias).join(", ")} at an old address (${stale[0].current}); models now listen on ${stale[0].expected}. `,
        el("button", { class: "small", onclick: repairEndpoints }, "Fix in Turnstone")]
      : [el("span", { class: "pill ok" }, "endpoints OK"), ` ${list.length} registered model(s) point at the current WSL adapter address.`]);
  } catch (e) { fill(box, el("span", { class: "error" }, `Turnstone endpoint check failed: ${e.message}`)); }
}

async function repairEndpoints() {
  try {
    const r = await api("/api/turnstone/repair", { method: "POST" });
    toast(`Updated ${r.fixed.length} model definition(s) in Turnstone`);
  } catch (e) { toast(e.message, 10000); }
  checkEndpoints();
}

async function act(name, action) {
  const labels = { start: "Starting", stop: "Stopping", benchmark: "Benchmarking", register: "Registering", "reach-test": "Testing reach from Turnstone for" };
  toast(`${labels[action]} ${name}...`, 60000);
  try {
    const r = await api(`/api/local/${encodeURIComponent(name)}/${action}`, { method: "POST" });
    if (action === "benchmark") toast(`${name}: ${fmt.tps(r.decode_tokens_per_s)} generation, ${fmt.tps(r.prefill_tokens_per_s)} prompt`, 8000);
    else if (action === "reach-test") toast(`${name}: ${r.ok ? "reachable" : "NOT reachable"} at ${r.url} from ${r.from}${r.ok ? "" : ` (${r.detail})`}`, 12000);
    else if (action === "start") toast(`${name}: loading on port ${r.port}; large models take a few minutes`, 8000);
    else toast(`${name}: ${r.status || "done"}`);
  } catch (e) { toast(`${name}: ${e.message}`, 10000); }
  setTimeout(() => { loadInstalled(); loadResources(); }, action === "start" ? 4000 : 500);
}

// ---------- successors ----------
async function loadSuccessors() {
  const box = $("#successors");
  box.className = "loading"; box.textContent = "Checking publishers on Hugging Face...";
  try {
    const fams = await api("/api/discover/successors");
    fams.sort((a, b) => (b.installed - a.installed) || ((b.successors.length + b.latest.length) - (a.successors.length + a.latest.length)));
    const cards = fams.filter((f) => f.successors.length || f.latest.length || f.installed).map((f) => {
      const list = f.installed ? f.successors : f.latest;
      return el("div", { class: "family" },
        el("h3", {}, f.label, " ", f.installed ? el("span", { class: "pill ok" }, "installed") : el("span", { class: "pill" }, "not installed")),
        el("div", { class: "small muted" }, f.installed ? (list.length ? `Newer than your newest (${fmt.date(f.installed_newest)}):` : `You have the latest (${fmt.date(f.installed_newest)}).`) : "Latest releases:"),
        el("ul", {}, list.map((m) => el("li", {},
          repoLink(m.id), " ", el("span", { class: "small muted" }, `${fmt.date(m.created)} · ${fmt.params(m.params)} · ${fmt.num(m.downloads)} downloads`),
          el("div", {}, m.gguf ? [el("span", { class: "small" }, "GGUF: "), el("a", { href: "#", onclick: (ev) => { ev.preventDefault(); openRepo(m.gguf.id); } }, m.gguf.id), " ", publisherPill(m.gguf.publisher)]
            : el("span", { class: "small muted" }, "no GGUF conversion yet"))))));
    });
    box.className = "grid"; fill(box, ...cards);
  } catch (e) { errorBox(box, e); }
}

// ---------- popular ----------
async function loadPopular() {
  const box = $("#popular");
  box.className = "loading"; box.textContent = "Loading Hugging Face and GitHub...";
  try {
    const p = await api("/api/discover/popular");
    const hfTable = (title, items) => [el("h3", {}, title), Array.isArray(items) ? table(["Model", "Task", "Downloads", "Likes", "Created", "Publisher", ""],
      items.map((m) => el("tr", {}, el("td", {}, repoLink(m.id)), el("td", { class: "small" }, m.pipeline_tag || "–"),
        el("td", { class: "num" }, fmt.num(m.downloads)), el("td", { class: "num" }, fmt.num(m.likes)), el("td", { class: "num" }, fmt.date(m.created)),
        el("td", {}, publisherPill(m.publisher, m.modified)),
        el("td", {}, m.tags?.includes("gguf") ? el("button", { class: "small ghost", onclick: () => openRepo(m.id) }, "Open") : null)))) : el("div", { class: "empty error" }, items.error)];
    const ghTable = (title, items) => [el("h3", {}, title), Array.isArray(items) ? table(["Repository", "Description", "Stars", "Updated", "Licence"],
      items.map((r) => el("tr", {}, el("td", {}, repoLink(r.id, "github")), el("td", { class: "small" }, r.description || ""),
        el("td", { class: "num" }, fmt.num(r.stars)), el("td", { class: "num" }, fmt.date(r.updated)), el("td", { class: "small" }, r.license || "–")))) : el("div", { class: "empty error" }, items.error)];
    box.className = "";
    fill(box, 
      ...hfTable("Trending GGUF models (Hugging Face)", p.hf_trending_gguf),
      ...hfTable("Most-downloaded GGUF models (Hugging Face, last 30 days)", p.hf_most_downloaded_gguf),
      ...hfTable("Trending text-generation models (Hugging Face, any format)", p.hf_trending),
      ...ghTable("Rising LLM projects (GitHub, created in the last 90 days)", p.github_rising_llm),
      ...ghTable("Most-starred local-LLM projects (GitHub)", p.github_local_llm));
  } catch (e) { errorBox(box, e); }
}

// ---------- watchlist ----------
async function loadWatchlist() {
  const box = $("#watchlist");
  box.className = "loading"; box.textContent = "Evaluating candidates against this PC...";
  try {
    const items = await api("/api/discover/watchlist");
    const rows = items.map((c) => {
      const b = c.best_fit, e = b?.estimate;
      return el("tr", {},
        el("td", {}, el("div", { class: "name" }, repoLink(c.id)), el("div", { class: "small muted" }, c.note)),
        el("td", { class: "num" }, fmt.params(c.params), e?.active_params && e.active_params !== c.params ? el("div", { class: "small muted" }, `${fmt.params(e.active_params)} active`) : null),
        el("td", { class: "num" }, fmt.date(c.created)),
        el("td", { class: "small" }, c.license || "–"),
        el("td", {}, c.gguf ? [repoLink(c.gguf.id), " ", publisherPill(c.gguf.publisher)] : el("span", { class: "muted" }, "none yet")),
        el("td", {}, b ? `${b.quant} · ${fmt.gb(b.size_gb)}` : el("span", { class: "muted" }, c.smallest_gb ? `smallest ${fmt.gb(c.smallest_gb, 0)}` : "–")),
        el("td", { class: "num" }, e ? fmt.gb(e.memory_needed_gb) : "–"),
        el("td", {}, b ? fitPill(e.fit, e.fits_now) : fitPill("does not fit")),
        el("td", { class: "num" }, e ? fmt.tps(e.decode_tokens_per_s) : "–"),
        el("td", { class: "num" }, e ? fmt.secs(e.agent_turn.seconds) : "–"),
        el("td", {}, c.gguf ? el("button", { class: "small ghost", onclick: () => openRepo(c.gguf.id) }, "Open") : null));
    });
    box.className = "";
    fill(box, table(["Model", "Params", "Released", "Licence", "GGUF repo", "Suggested file", "Memory", "Fit", "Speed (est.)", "Agent turn", ""], rows),
      el("p", { class: "muted small" }, "Suggested file = the largest 3.5–6.6-bit quant that fits in dedicated GPU memory; otherwise the ~4.6-bit quant that fits in GPU-usable memory (run alone); otherwise the largest file that fits."));
  } catch (e) { errorBox(box, e); }
}

// ---------- search & repo detail ----------
async function runSearch(ev) {
  ev?.preventDefault();
  const q = $("#search-q").value.trim(), source = $("#search-source").value, gguf = $("#search-gguf").checked ? "1" : "0";
  const box = $("#search-results");
  if (!q) return;
  box.className = "loading"; box.textContent = "Searching...";
  fill($("#repo-detail"), );
  try {
    const results = await api(`/api/search?q=${encodeURIComponent(q)}&source=${source}&gguf=${gguf}`);
    box.className = "";
    if (!results.length) { box.className = "empty"; box.textContent = "No results."; return; }
    if (source === "github") {
      fill(box, table(["Repository", "Description", "Stars", "Updated", ""], results.map((r) => el("tr", {},
        el("td", {}, repoLink(r.id, "github")), el("td", { class: "small" }, r.description || ""), el("td", { class: "num" }, fmt.num(r.stars)),
        el("td", { class: "num" }, fmt.date(r.updated)), el("td", {}, el("button", { class: "small ghost", onclick: () => loadReleases(r.id) }, "Releases"))))));
    } else {
      fill(box, table(["Model", "Task", "Downloads", "Likes", "Updated", "Publisher", ""], results.map((m) => el("tr", {},
        el("td", {}, repoLink(m.id, source), m.gated ? el("span", { class: "pill warn" }, "gated") : null),
        el("td", { class: "small" }, m.pipeline_tag || "–"), el("td", { class: "num" }, fmt.num(m.downloads)), el("td", { class: "num" }, fmt.num(m.likes)),
        el("td", { class: "num" }, fmt.date(m.updated || m.created)), el("td", {}, publisherPill(m.publisher, m.modified)),
        el("td", {}, el("button", { class: "small ghost", onclick: () => loadRepo(m.id, source) }, "Open"))))));
    }
  } catch (e) { errorBox(box, e); }
}

async function loadRepo(id, source = "huggingface") {
  const box = $("#repo-detail");
  box.className = "loading"; box.textContent = `Reading ${id}...`;
  box.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const r = await api(`/api/repo?id=${encodeURIComponent(id)}&source=${source}`);
    const variants = r.variants.filter((v) => !v.extras);
    const extras = r.variants.find((v) => v.extras);
    const info = el("dl", { class: "kv" },
      el("dt", {}, "Used for"), el("dd", {}, (r.used_for || []).map((u) => el("span", { class: "pill" }, u))),
      el("dt", {}, "Parameters"), el("dd", {}, fmt.params(r.params || r.gguf_params)),
      el("dt", {}, "Architecture"), el("dd", {}, r.architecture || "–"),
      el("dt", {}, "Max context"), el("dd", {}, r.context_length ? fmt.num(r.context_length) + " tokens" : "–"),
      el("dt", {}, "Tool calling"), el("dd", {}, r.supports_tools === true ? "yes (chat template supports tools)" : r.supports_tools === false ? "no tool support in chat template" : "unknown"),
      el("dt", {}, "Licence"), el("dd", {}, r.license || "–"),
      el("dt", {}, "Base model"), el("dd", {}, (r.base_models || []).length ? r.base_models.map((b) => repoLink(b)) : "–"),
      el("dt", {}, "Publisher"), el("dd", {}, publisherPill(r.publisher, r.modified)),
      el("dt", {}, "Popularity"), el("dd", {}, `${fmt.num(r.downloads)} downloads · ${fmt.num(r.likes)} likes`),
      el("dt", {}, "Estimates use"), el("dd", {}, r.config_from || "heuristics (no config found)"));
    const rows = variants.map((v) => {
      const e = v.estimate;
      return el("tr", { class: v.recommended ? "recommended" : "" },
        el("td", {}, el("div", { class: "name" }, v.variant), v.recommended ? el("span", { class: "pill ok" }, "suggested") : null,
          v.parts > 1 ? el("span", { class: "pill" }, `${v.parts} parts`) : null),
        el("td", {}, v.quant), el("td", { class: "num" }, fmt.b(v.size)),
        el("td", { class: "num" }, e.bits_per_weight ? e.bits_per_weight.toFixed(2) : "–"),
        el("td", { class: "num" }, fmt.gb(e.memory_needed_gb)), el("td", {}, fitPill(e.fit, e.fits_now)),
        el("td", { class: "num" }, fmt.tps(e.decode_tokens_per_s)), el("td", { class: "num" }, fmt.secs(e.agent_turn.seconds)),
        el("td", {}, v.safe_files ? el("span", { class: "pill ok" }, "safe type") : el("span", { class: "pill bad" }, "unsafe type"),
          v.checksums ? el("span", { class: "pill ok" }, "sha256") : el("span", { class: "pill bad" }, "no checksum")),
        el("td", {}, el("button", { class: "small", disabled: !(v.safe_files && v.checksums) || e.fit === "does not fit",
          onclick: () => downloadVariant(r, v, source) }, "Download")));
    });
    box.className = "";
    fill(box, 
      el("div", { class: "detail-head" }, el("h3", {}, repoLink(r.id, source)), el("span", { class: "muted small" }, `updated ${fmt.date(r.updated)}`)),
      info,
      el("h3", {}, "Files"),
      variants.length ? table(["Variant", "Quant", "Download size", "Bits/weight", "Memory needed", "Fit", "Speed (est.)", "Agent turn", "Safety", ""], rows)
        : el("div", { class: "empty" }, r.safetensors_gb ? `No GGUF files. This repo has ${fmt.gb(r.safetensors_gb)} of safetensors weights for other runtimes (vLLM/transformers); search for a GGUF conversion instead.` : "No GGUF files in this repository."),
      extras ? el("p", { class: "muted small" }, `Also in this repo (not downloaded automatically): ${extras.files.map((f) => `${f.path.split("/").pop()} (${f.kind}, ${fmt.b(f.size)})`).join(", ")}`) : null);
  } catch (e) { errorBox(box, e); }
}

async function downloadVariant(repo, v, source) {
  const suggested = v.variant.toLowerCase().replace(/[^a-z0-9.]+/g, "-").slice(0, 60);
  const nameInput = el("input", { value: suggested, "aria-label": "Model name", style: "width:100%" });
  const makeAvail = el("input", { type: "checkbox", checked: true });
  const free = lastSystem?.disk.available_gb;
  const ok = await confirmDialog(`Download ${v.variant}?`, [
    el("dl", { class: "kv" },
      el("dt", {}, "From"), el("dd", {}, `${repo.id} (${source})`),
      el("dt", {}, "Files"), el("dd", {}, v.files.map((f) => f.path.split("/").pop()).join(", ")),
      el("dt", {}, "Size"), el("dd", {}, fmt.b(v.size) + (free ? ` (disk has ${fmt.gb(free, 0)} free)` : "")),
      el("dt", {}, "Memory to run"), el("dd", {}, `${fmt.gb(v.estimate.memory_needed_gb)} · ${v.estimate.fit}`),
      el("dt", {}, "Verification"), el("dd", {}, "SHA-256 checked against the repository after download")),
    el("p", {}, "Name in models.json and Turnstone:"), nameInput,
    el("label", { class: "check" }, makeAvail, " When finished, start it, test that Turnstone can reach it and register it in Turnstone"),
    el("p", { class: "small muted" }, "Saved to llm\\models (served to Turnstone by llama-server). Registration needs a Turnstone token and Turnstone running.")]);
  if (!ok) return;
  try {
    await api("/api/downloads", { method: "POST", body: { source, repo: repo.id, variant: v.variant, name: nameInput.value.trim() || suggested, make_available: makeAvail.checked } });
    toast(`Queued ${v.variant}`);
    selectTab("downloads");
  } catch (e) { toast(e.message, 10000); }
}

async function loadReleases(repo) {
  const box = $("#repo-detail");
  box.className = "loading"; box.textContent = `Reading releases of ${repo}...`;
  try {
    const rels = await api(`/api/github/releases?repo=${encodeURIComponent(repo)}`);
    box.className = "";
    fill(box, el("h3", {}, "Releases of ", repoLink(repo, "github")),
      ...rels.map((rel) => el("div", {}, el("h3", {}, `${rel.tag} `, el("span", { class: "small muted" }, fmt.date(rel.published)), rel.prerelease ? el("span", { class: "pill warn" }, "pre-release") : null),
        rel.assets.length ? table(["Asset", "Size", "Safety", "Checksum", ""], rel.assets.map((a) => el("tr", {},
          el("td", {}, a.name), el("td", { class: "num" }, fmt.b(a.size)),
          el("td", {}, el("span", { class: `pill ${a.verdict === "ok" ? "ok" : "bad"}`, title: a.why }, a.verdict === "ok" ? "model weights" : "blocked")),
          el("td", {}, a.sha256 ? el("span", { class: "pill ok" }, "sha256") : el("span", { class: "pill bad" }, "none")),
          el("td", {}, el("button", { class: "small", disabled: a.verdict !== "ok" || !a.sha256, onclick: () => downloadAsset(repo, rel.tag, a) }, "Download")))))
          : el("div", { class: "empty" }, "No assets."))));
  } catch (e) { errorBox(box, e); }
}

async function downloadAsset(repo, tag, a) {
  const ok = await confirmDialog(`Download ${a.name}?`, [el("p", {}, `${fmt.b(a.size)} from ${repo}@${tag}, SHA-256 verified after download.`)]);
  if (!ok) return;
  try { await api("/api/downloads", { method: "POST", body: { source: "github", repo, tag, asset: a.name } }); toast(`Queued ${a.name}`); selectTab("downloads"); }
  catch (e) { toast(e.message, 10000); }
}

// ---------- estimator ----------
async function loadEstimator() {
  const box = $("#estimator");
  const defaults = await api("/api/info").catch(() => ({ estimates: {} }));
  const d = defaults.estimates || {};
  const form = el("form", { class: "row", id: "est-form" },
    el("label", {}, "Context per request ", el("input", { id: "est-ctx", type: "number", min: 1024, step: 1024, value: d.default_ctx || 65536, style: "width:110px" })),
    el("label", {}, "Parallel requests ", el("input", { id: "est-par", type: "number", min: 1, max: 16, value: d.default_parallel || 2, style: "width:70px" })),
    el("label", {}, "Prompt tokens / turn ", el("input", { id: "est-p", type: "number", min: 1, value: d.agent_turn_prompt_tokens || 6000, style: "width:100px" })),
    el("label", {}, "Output tokens / turn ", el("input", { id: "est-o", type: "number", min: 1, value: d.agent_turn_output_tokens || 800, style: "width:90px" })),
    el("button", { type: "submit" }, "Calculate"));
  const results = el("div", { id: "est-results", class: "loading" }, "Calculating...");
  box.className = "";
  fill(box, form, results,
    el("p", { class: "muted small" }, "A Turnstone agent turn sends the conversation, tool definitions and results as the prompt, then generates a reply or tool call. The LLM judge (if enabled) adds a smaller call per tool use. See docs/ASSUMPTIONS.md for the formulas."));
  form.addEventListener("submit", (ev) => { ev.preventDefault(); runEstimate(); });
  runEstimate();
}

async function runEstimate() {
  const out = $("#est-results");
  const qs = new URLSearchParams({ ctx: $("#est-ctx").value, parallel: $("#est-par").value, prompt: $("#est-p").value, output: $("#est-o").value });
  out.className = "loading"; out.textContent = "Calculating...";
  try {
    const r = await api(`/api/compare?${qs}`);
    const cur = r.pricing.currency || "USD";
    const localRows = r.local.map((m) => {
      const e = m.estimate, t = e.agent_turn;
      return el("tr", {}, el("td", {}, el("div", { class: "name" }, m.name), el("span", { class: "pill" }, "local")),
        el("td", { class: "num" }, fmt.gb(e.memory_needed_gb), el("div", { class: "small muted" }, `weights ${fmt.gb(e.weights_gb)} + KV ${fmt.gb(e.kv_cache_gb)}`)),
        el("td", {}, fitPill(e.fit, e.fits_now)),
        el("td", { class: "num" }, fmt.tps(e.best_decode_tokens_per_s), el("div", { class: "small muted" }, e.best_decode_source)),
        el("td", { class: "num" }, fmt.tps(e.prefill_tokens_per_s)),
        el("td", { class: "num" }, fmt.secs(t.seconds_best || t.seconds)),
        el("td", { class: "num" }, t.seconds_best ? fmt.num(Math.round(3600 / t.seconds_best * e.parallel)) : "–"),
        el("td", { class: "num" }, t.energy_cost !== null && t.energy_cost !== undefined ? fmt.money(t.energy_cost, cur) + " power" : "free (set electricity price for power cost)"));
    });
    const cloudRows = r.cloud.map((c) => el("tr", {},
      el("td", {}, el("div", { class: "name" }, c.alias), el("span", { class: "pill info" }, `${c.provider} · ${c.model}`)),
      el("td", { class: "num" }, "none (cloud)"), el("td", {}, el("span", { class: "pill ok" }, "n/a")),
      el("td", { class: "num" }, fmt.tps(c.tokens_per_second)), el("td", { class: "num" }, "–"),
      el("td", { class: "num" }, fmt.secs(c.agent_turn.seconds)), el("td", { class: "num" }, "–"),
      el("td", { class: "num" }, c.agent_turn.cost !== null ? `${fmt.money(c.agent_turn.cost, cur)} / turn · ${fmt.money(c.agent_turn.cost_per_1000_turns, cur)} / 1000` : "set prices in config/pricing.json")));
    out.className = "";
    fill(out, table(["Model", "Memory needed", "Fit", "Generation", "Prompt reading", "Time per agent turn", "Turns per hour", "Cost per turn"], [...localRows, ...cloudRows]),
      el("p", { class: "muted small" }, r.calibration.samples ? `Unbenchmarked local speeds are scaled by ${r.calibration.factor.toFixed(2)} from ${r.calibration.samples} benchmark(s).` : "Benchmark running models on the Installed tab to replace estimates with measurements."));
  } catch (e) { errorBox(out, e); }
}

// ---------- sources ----------
async function loadSources() {
  const box = $("#sources");
  try {
    const r = await api("/api/sources");
    fill(box, table(["Source", "Hosts", "Trust", "Checksums", ""], r.sources.map((s) => el("tr", {},
      el("td", {}, el("div", { class: "name" }, s.name), el("div", { class: "small muted" }, s.base_url)),
      el("td", { class: "small" }, s.hosts.join(", ")),
      el("td", {}, el("span", { class: `pill ${s.trust === "trusted" ? "ok" : s.trust === "reviewed" ? "info" : "bad"}` }, s.trust)),
      el("td", { class: "small" }, s.checksum || "–"),
      el("td", {}, el("select", { "aria-label": `Trust for ${s.name}`, onchange: (ev) => setTrust(s.id, ev.target.value) },
        ["trusted", "reviewed", "blocked"].map((t) => el("option", { value: t, selected: t === s.trust }, t))))))),
      el("p", { class: "muted small" }, `Publisher tiers: official = ${r.trusted_publishers.official.length} labs, known quantizers = ${r.trusted_publishers.quantizers.join(", ")}. Everything else shows as "community".`));
  } catch (e) { errorBox(box, e); }
}

async function setTrust(id, trust) {
  try { await api(`/api/sources/${encodeURIComponent(id)}/trust`, { method: "POST", body: { trust } }); toast(`${id}: ${trust}`); }
  catch (e) { toast(e.message, 8000); }
  loadSources();
}

async function checkSource(ev) {
  ev.preventDefault();
  const url = $("#source-url").value.trim(), name = $("#source-name").value.trim();
  const box = $("#source-result");
  box.className = "loading"; box.textContent = "Running safety checks...";
  try {
    const r = await api("/api/sources/check", { method: "POST", body: { url } });
    const cls = r.verdict === "safe" ? "ok" : r.verdict === "caution" ? "warn" : "bad";
    const isFile = /\.(gguf|safetensors)$/i.test(new URL(url).pathname);
    const actions = [];
    if (r.verdict !== "unsafe" && !r.known_source)
      actions.push(el("button", { onclick: () => addSource(url, name) }, `Add ${r.host} as a reviewed source`));
    if (r.verdict !== "unsafe" && isFile) {
      const sha = el("input", { placeholder: "SHA-256 of the file (64 hex characters)", pattern: "[0-9a-fA-F]{64}", style: "flex:1 1 360px" });
      const nm = el("input", { placeholder: "Model name" });
      actions.push(el("div", { class: "row" }, sha, nm, el("button", { class: "ghost", onclick: () => directDownload(url, sha.value.trim(), nm.value.trim()) }, "Download file")));
    }
    box.className = "";
    fill(box, el("h3", {}, "Verdict: ", el("span", { class: `pill ${cls}` }, r.verdict), " ", r.host),
      el("ul", { class: "checks" }, r.checks.map((c) => el("li", { class: c.ok ? "ok" : "bad" }, el("b", {}, c.check), ` - ${c.detail}`))),
      r.redirects.length ? el("p", { class: "small muted" }, "Redirects: " + r.redirects.map((h) => `${h.url} (${h.status})`).join(" -> ")) : null,
      ...actions);
  } catch (e) { errorBox(box, e); }
}

async function addSource(url, name) {
  try { await api("/api/sources", { method: "POST", body: { url, name } }); toast("Source added as reviewed"); loadSources(); }
  catch (e) { toast(e.message, 10000); }
}

async function directDownload(url, sha256, name) {
  if (!/^[0-9a-fA-F]{64}$/.test(sha256)) { toast("Enter the file's SHA-256 (64 hex characters) from the publisher", 8000); return; }
  try { await api("/api/downloads", { method: "POST", body: { source: "direct", url, sha256, name } }); toast("Download queued"); selectTab("downloads"); }
  catch (e) { toast(e.message, 10000); }
}

// ---------- downloads ----------
let dlTimer = null;
async function loadDownloads() {
  const box = $("#downloads");
  try {
    const jobs = await api("/api/downloads");
    const active = jobs.filter((j) => j.status === "queued" || j.status === "downloading");
    const badge = $("#dl-badge");
    badge.textContent = active.length; badge.classList.toggle("hidden", !active.length);
    if (!jobs.length) { box.className = "empty"; box.textContent = "No downloads yet."; }
    else {
      box.className = "";
      fill(box, table(["Download", "Status", "Progress", "Speed", "Time left", ""], jobs.map((j) => {
        const left = j.speed_bps > 0 ? (j.total_bytes - j.done_bytes) / j.speed_bps : null;
        const cls = j.status === "verified" ? "ok" : j.status === "failed" ? "bad" : j.status === "cancelled" ? "" : "info";
        return el("tr", {},
          el("td", {}, el("div", { class: "name" }, j.title), el("div", { class: "small muted" }, j.files.join(", "))),
          el("td", {}, el("span", { class: `pill ${cls}` }, j.status), j.error ? el("div", { class: "small error" }, j.error) : null,
            j.result?.registered ? el("div", { class: "small muted" }, `added as ${j.result.registered.name} (port ${j.result.registered.port})`) : null),
          el("td", { style: "min-width:180px" }, bar(j.done_bytes, j.total_bytes || 1), el("div", { class: "small muted" }, `${fmt.b(j.done_bytes)} / ${fmt.b(j.total_bytes)}`, j.current ? ` · ${j.current}` : "")),
          el("td", { class: "num" }, j.status === "downloading" && j.speed_bps ? `${(j.speed_bps / 1e6).toFixed(1)} MB/s` : "–"),
          el("td", { class: "num" }, j.status === "downloading" ? fmt.secs(left) : "–"),
          el("td", {}, (j.status === "queued" || j.status === "downloading") ? el("button", { class: "small ghost", onclick: () => cancelDownload(j.id) }, "Cancel") : null));
      })));
    }
    clearTimeout(dlTimer);
    if (active.length) dlTimer = setTimeout(loadDownloads, 2000);
  } catch (e) { errorBox(box, e); }
}

async function cancelDownload(id) {
  try { await api(`/api/downloads/${id}/cancel`, { method: "POST" }); } catch (e) { toast(e.message); }
  loadDownloads();
}

// ---------- tabs ----------
const loaders = { installed: loadInstalled, successors: loadSuccessors, popular: loadPopular, watchlist: loadWatchlist,
  estimator: loadEstimator, sources: loadSources, downloads: loadDownloads };
const loaded = new Set();

function selectTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  try { localStorage.setItem("modelhub.tab", name); } catch { /* storage unavailable */ }
  if (loaders[name] && (!loaded.has(name) || name === "downloads" || name === "installed")) { loaded.add(name); loaders[name](); }
}

document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));
$("#search-form").addEventListener("submit", runSearch);
$("#open-form").addEventListener("submit", (ev) => { ev.preventDefault(); const v = $("#open-repo").value.trim(); if (v) loadRepo(v, $("#search-source").value === "modelscope" ? "modelscope" : "huggingface"); });
$("#source-form").addEventListener("submit", checkSource);
$("#refresh").addEventListener("click", () => {
  loaded.clear(); loadResources();
  const active = document.querySelector(".tabs button.active")?.dataset.tab || "installed";
  selectTab(active);
});

// Monitoring: restore the user's last choice. Continuous is the default on first visit.
$("#monitor-bar").addEventListener("submit", startMonitor);
$("#mon-stop").addEventListener("click", stopMonitor);
$("#mon-mode").addEventListener("change", () => { syncMonitorForm(); if ($("#mon-mode").value === "stopped") stopMonitor(); });
(async () => {
  const saved = monSettings();
  if (saved.mode) $("#mon-mode").value = saved.mode;
  if (saved.dur) $("#mon-dur").value = saved.dur;
  if (saved.unit) $("#mon-unit").value = saved.unit;
  if (saved.interval) $("#mon-int").value = saved.interval;
  syncMonitorForm();
  await loadResources();
  loadHardware();
  try {
    const st = await api("/api/monitor");
    if (st.running) { $("#mon-mode").value = st.mode; syncMonitorForm(); pollMonitor(); }
    else if (($("#mon-mode").value || "continuous") === "continuous") startMonitor();
    else renderMonitor(st);
  } catch { /* monitor unavailable */ }
})();
let startTab = "installed";
try { startTab = localStorage.getItem("modelhub.tab") || "installed"; } catch { /* ignore */ }
selectTab(loaders[startTab] || startTab === "search" || startTab === "export" ? startTab : "installed");
loadDownloads();
