// Checkpoint UI — plain JS, no build step.
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = (n) => (n === null || n === undefined || isNaN(n)) ? "—" : "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const when = (iso) => iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 2800);
}

const STAGES = [
  ["intake", "Receive document", false], ["extract", "Read invoice", true], ["validate", "Validate fields & arithmetic", false],
  ["vendor", "Verify vendor", false], ["duplicate", "Check for duplicates", false], ["po", "Match purchase order", false],
  ["amount", "Check amounts against PO", false], ["decide", "Decide", false], ["explain", "Explain", true],
];
const SAMPLE_TITLES = {
  "01": "Clean invoice, exact PO match", "02": "Scanned, tax hidden inside prices", "03": "Split PO — 3rd instalment overruns",
  "04": "Disguised duplicate (scanned re-send)", "05": "No PO number, two POs fit", "06": "Perfect invoice, new bank account",
  "07": "Blocked vendor", "08": "Monthly retainer — same amount as last month", "09": "Totals don't add up, no invoice number",
};
const ICON = { pass: "✓", warn: "!", hold: "!", reject: "✕", skip: "–" };
const REVIEW_LABEL = { pending: "Awaiting review", approved: "Approved by reviewer", rejected: "Rejected by reviewer", superseded: "Re-checked with chosen PO" };
let RUN_CTX = {};

let timers = [];
let currentSource = null;
function clearPage() { timers.forEach(clearInterval); timers = []; if (currentSource) { currentSource.close(); currentSource = null; } }

// ------------------------------------------------------------------ router
const routes = { "": viewNew, dashboard: viewDashboard, review: viewReview, reference: viewReference, rules: viewRules, run: viewRun };
async function route() {
  clearPage();
  const [, name = "", arg] = location.hash.split("/");
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === (name || "new")));
  window.scrollTo(0, 0);
  await (routes[name] || viewNew)(arg);
  refreshBadge();
}
window.addEventListener("hashchange", route);

async function refreshBadge() {
  try {
    const m = await api("/api/metrics");
    const b = $("#reviewBadge"); b.textContent = m.pending_review; b.hidden = !m.pending_review;
  } catch {}
}
async function health() {
  const h = $("#health");
  try {
    const s = await api("/api/health");
    const names = { groq: "Groq", gemini: "Gemini" };
    if (s.extractor !== "extract_invoice") { h.className = "health off"; h.textContent = "Offline test mode"; }
    else if (s.providers.length) { h.className = "health on"; h.textContent = "AI: " + s.providers.map((p) => names[p] || p).join(" → backup ") ; h.title = "AI providers, in the order they are tried"; }
    else { h.className = "health off"; h.textContent = "No AI key set"; }
  } catch { h.className = "health off"; h.textContent = "Server unreachable"; }
}

// ------------------------------------------------------------------ new run
async function viewNew() {
  const samples = await api("/api/samples");
  const group = (p) => samples.filter((s) => s.file.includes(`_${p}`));
  const card = (s) => `
    <div class="card sample">
      <div class="t">${esc(SAMPLE_TITLES[s.file.slice(0, 2)] || s.file)}</div>
      <div class="d">${esc(s.scenario)}</div>
      <div class="row">
        <button class="primary small" data-run="${esc(s.file)}">Run</button>
        <a class="btn small" href="/api/samples/${encodeURIComponent(s.file)}" target="_blank">View PDF</a>
        ${s.scanned ? '<span class="tag scan">scanned</span>' : ""}
      </div>
    </div>`;
  $("#view").innerHTML = `
    <div class="page-head"><div>
      <h1>Check an invoice</h1>
      <div class="muted">Upload a vendor invoice PDF. Checkpoint reads it, checks it against the vendor master, the purchase orders and past invoices, and decides whether it can be paid.</div>
    </div></div>
    <label class="drop" id="drop">
      <input type="file" id="file" accept="application/pdf" hidden>
      <div class="big">Drop an invoice PDF here, or click to choose</div>
      <div class="muted">Digital or scanned · up to 10 MB</div>
    </label>
    <div class="group-title"><h2 style="margin:0">Happy path</h2><span class="muted">Should go straight through</span></div>
    <div class="samples">${group("happy").map(card).join("")}</div>
    <div class="group-title"><h2 style="margin:0">Edge cases</h2><span class="muted">Realistic situations where the process must behave differently</span></div>
    <div class="samples">${group("edge").map(card).join("")}</div>
    <div class="group-title"><h2 style="margin:0">Also handled</h2></div>
    <div class="samples">${group("extra").map(card).join("")}</div>`;
  const drop = $("#drop"), input = $("#file");
  const go = async (file) => {
    if (!file) return;
    const fd = new FormData(); fd.append("file", file);
    try { const { id } = await api("/api/runs", { method: "POST", body: fd }); location.hash = `#/run/${id}`; }
    catch (e) { toast(e.message); }
  };
  input.onchange = () => go(input.files[0]);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); go(e.dataTransfer.files[0]); };
  document.querySelectorAll("[data-run]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try { const { id } = await api(`/api/runs/sample/${encodeURIComponent(b.dataset.run)}`, { method: "POST" }); location.hash = `#/run/${id}`; }
    catch (e) { toast(e.message); b.disabled = false; }
  }));
}

// ------------------------------------------------------------------ run view
async function viewRun(id) {
  let run = await api(`/api/runs/${id}`);
  const state = { stages: {}, running: null, extraction: run.extraction, done: run.status !== "running", toggled: {} };
  RUN_CTX = { id: run.id, pending: run.review_status === "pending" };
  (run.stages || []).forEach((s) => (state.stages[s.stage] = s));

  $("#view").innerHTML = `
    <div class="page-head">
      <div><h1>${esc(run.filename)}</h1><div class="muted">Run #${run.id} · started ${when(run.created_at)} <span id="dur"></span></div></div>
      <div style="display:flex;gap:8px"><a class="btn" href="#/dashboard">All runs</a><a class="btn" href="#/">New run</a></div>
    </div>
    <div id="verdict"></div>
    <div class="run-grid">
      <div class="card"><div class="pad" style="padding-bottom:0"><h3>Pipeline</h3></div><div class="timeline" id="timeline"></div></div>
      <div class="card">
        <div class="tabs" id="tabs">
          <button data-tab="data" class="on">Extracted data</button><button data-tab="doc">Document</button><button data-tab="json">Audit trail (JSON)</button>
        </div>
        <div class="tab-body" id="tabBody"></div>
      </div>
    </div>`;

  let tab = "data";
  const renderTab = () => {
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
    const body = $("#tabBody");
    if (tab === "doc") body.innerHTML = `<iframe class="pdf" src="/api/runs/${id}/file"></iframe>`;
    else if (tab === "json") body.innerHTML = `<pre class="json">${esc(JSON.stringify(run, null, 2))}</pre>`;
    else body.innerHTML = renderExtraction(state.extraction, state.stages.extract);
  };
  document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => { tab = b.dataset.tab; renderTab(); }));

  const renderAll = () => {
    $("#timeline").innerHTML = STAGES.map(([k, label, ai]) => renderStage(k, label, ai, state)).join("");
    document.querySelectorAll(".stage-head").forEach((h) => (h.onclick = () => {
      const el = h.parentElement; el.classList.toggle("open"); state.toggled[el.dataset.key] = el.classList.contains("open");
    }));
    document.querySelectorAll("[data-usepo]").forEach((b) => (b.onclick = async () => {
      b.disabled = true;
      try { const { id: nid } = await api(`/api/runs/${run.id}/use-po`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ po_number: b.dataset.usepo }) });
        toast(`Re-checking against ${b.dataset.usepo}…`); location.hash = `#/run/${nid}`; }
      catch (e) { toast(e.message); b.disabled = false; }
    }));
  };
  const renderVerdict = () => {
    $("#verdict").innerHTML = state.done ? verdictHTML(run) :
      `<div class="verdict pending"><div class="word muted">PROCESSING</div><div class="headline">${esc(state.running ? (STAGES.find((s) => s[0] === state.running) || [, ""])[1] + "…" : "Starting…")}</div></div>`;
    if (state.done) bindReview(run, async () => { run = await api(`/api/runs/${id}`); renderVerdict(); refreshBadge(); });
    if (run.duration_ms) $("#dur").textContent = `· took ${(run.duration_ms / 1000).toFixed(1)}s`;
  };
  renderAll(); renderVerdict(); renderTab();
  if (state.done) return;

  // live: stream stage events; pace them slightly so each stage is visible (real timings are shown)
  const queue = [];
  let finished = false;
  const es = new EventSource(`/api/runs/${id}/events`); currentSource = es;
  es.onmessage = (m) => queue.push(JSON.parse(m.data));
  es.onerror = () => { es.close(); };
  while (!finished) {
    const ev = queue.shift();
    if (!ev) { await sleep(80); if (!document.body.contains($("#timeline"))) return; continue; }
    if (ev.type === "stage_start") { state.running = ev.stage; renderAll(); renderVerdict(); await sleep(ev.ai ? 0 : 220); }
    else if (ev.type === "stage_end") {
      state.stages[ev.stage] = ev; state.running = null;
      if (ev.stage === "extract") { state.extraction = ev.data?.extraction; if (tab === "data") renderTab(); }
      renderAll(); await sleep(260);
    } else if (ev.type === "done" || ev.type === "error") {
      finished = true; es.close(); currentSource = null;
      run = await api(`/api/runs/${id}`); state.done = true; state.extraction = run.extraction;
      RUN_CTX.pending = run.review_status === "pending";
      renderVerdict(); renderAll(); renderTab(); refreshBadge();
    }
  }
}

function renderStage(k, label, ai, state) {
  const s = state.stages[k];
  const status = s ? s.status : state.running === k ? "running" : "waiting";
  const flagged = s && /hold|reject/.test(s.status) && k !== "decide";
  const open = state.toggled[k] ?? flagged;
  const cls = (s ? "" : status === "waiting" ? "waiting" : "") + (open ? " open" : "");
  const icon = s ? ICON[s.status] : "";
  const findings = s ? s.findings.filter((f) => f.severity !== "pass" || s.findings.length === 1) : [];
  return `
  <div class="stage ${cls}" data-status="${status}" data-key="${k}">
    <div class="stage-head">
      <span class="ico ${status}">${icon}</span>
      <div><div class="name">${esc(label)} ${ai ? '<span class="tag ai">AI</span>' : ""}</div>
        <div class="sum">${s ? esc(s.summary) : status === "running" ? (ai ? "Calling Gemini…" : "Running…") : ""}</div></div>
      <span class="ms">${s ? s.ms + " ms" : ""}</span>
    </div>
    ${s ? `<div class="stage-body">${findings.map(findingHTML).join("") || '<div class="muted">No issues.</div>'}${stageExtra(k, s)}</div>` : ""}
  </div>`;
}

function findingHTML(f) {
  return `<div class="finding"><span class="sev ${f.severity}">${f.severity}</span><div>
    <div class="ft">${esc(f.title)}</div>${f.detail ? `<div class="fd">${esc(f.detail)}</div>` : ""}
    ${f.action && f.severity !== "info" ? `<div class="fa">${esc(f.action)}</div>` : ""}
    ${findingExtra(f)}</div></div>`;
}

function findingExtra(f) {
  const d = f.data || {};
  if (f.code === "PO_AMBIGUOUS" || f.code === "PO_INFERRED") {
    return `<table class="mini-table"><tr><th>Candidate PO</th><th>Description</th><th class="num">Remaining</th><th class="num">Desc.</th><th class="num">Price</th><th class="num">Score</th>${f.code === "PO_AMBIGUOUS" && RUN_CTX.pending ? "<th></th>" : ""}</tr>
      ${(d.candidates || []).map((c, i) => `<tr class="${i === 0 ? "best" : ""}"><td class="mono">${esc(c.po_number)}</td><td>${esc(c.description)}</td>
      <td class="num">${inr(c.remaining)}</td><td class="num">${c.breakdown.description}</td><td class="num">${c.breakdown.unit_price}</td><td class="num"><b>${c.score}</b></td>
      ${f.code === "PO_AMBIGUOUS" && RUN_CTX.pending ? `<td><button class="small primary" data-usepo="${esc(c.po_number)}">Use PO</button></td>` : ""}</tr>`).join("")}</table>`;
  }
  if (f.code === "PO_OVERRUN" || f.code === "AMOUNT_OK" || f.code === "WITHIN_TOLERANCE") return balanceHTML(d);
  if (f.code === "BANK_CHANGED") {
    const a = d.invoice || {}, m = d.master || {};
    return `<div class="compare"><span class="h"></span><span class="h">On invoice</span><span class="h">Vendor master</span>
      <span>Account</span><span class="mono ${a.account && a.account !== m.account ? "diff" : ""}">${esc(a.account)}</span><span class="mono">${esc(m.account)}</span>
      <span>IFSC</span><span class="mono ${a.ifsc && a.ifsc !== m.ifsc ? "diff" : ""}">${esc(a.ifsc)}</span><span class="mono">${esc(m.ifsc)}</span></div>`;
  }
  if (f.code === "PROBABLE_DUPLICATE" || f.code === "EXACT_DUPLICATE") {
    const m = d.match || {};
    return `<div class="compare"><span class="h"></span><span class="h">Already in ledger</span><span class="h"></span>
      <span>Invoice no.</span><span class="mono">${esc(m.invoice_number)}</span><span></span>
      <span>Date</span><span>${esc(m.invoice_date)}</span><span></span>
      <span>Amount</span><span>${inr(m.total)}</span><span></span>
      <span>Status</span><span>${esc(m.status)}${m.paid_on ? " · " + esc(m.paid_on) : ""}</span><span></span></div>`;
  }
  return "";
}

function balanceHTML(d) {
  if (!d || d.po_subtotal === undefined) return "";
  const po = d.po_subtotal, before = Math.min(d.invoiced_before, po), mine = d.this_invoice || 0;
  const fitting = Math.max(0, Math.min(mine, po - before)), excess = Math.max(0, mine - Math.max(d.remaining, 0));
  const scale = Math.max(po, before + mine);
  const pct = (x) => (100 * x / scale).toFixed(2) + "%";
  return `<div class="balance"><div class="bar">
      <span class="seg" style="width:${pct(before)};background:#9aa5b5"></span>
      <span class="seg" style="width:${pct(fitting)};background:var(--accent)"></span>
      <span class="seg" style="width:${pct(excess)};background:var(--bad)"></span></div>
    <div class="legend"><span><i style="background:#9aa5b5"></i>Invoiced before ${inr(d.invoiced_before)}</span>
      <span><i style="background:var(--accent)"></i>This invoice, within PO ${inr(fitting)}</span>
      ${excess ? `<span><i style="background:var(--bad)"></i>Over PO ${inr(excess)}</span>` : ""}
      <span class="muted">PO value ${inr(po)} (pre-tax)</span></div>
    ${d.prior_invoices && d.prior_invoices.length ? `<table class="mini-table"><tr><th>Earlier invoices on this PO</th><th>Date</th><th class="num">Pre-tax</th></tr>
      ${d.prior_invoices.map((p) => `<tr><td class="mono">${esc(p.invoice_number)}</td><td>${esc(p.invoice_date)}</td><td class="num">${inr(p.subtotal)}</td></tr>`).join("")}</table>` : ""}</div>`;
}

function stageExtra(k, s) {
  const d = s.data || {};
  if (k === "intake") return `<div class="muted" style="font-size:12.5px">SHA-256 ${esc(d.sha256)}… · ${(d.bytes / 1024).toFixed(0)} KB</div>`;
  if (k === "extract" && d.model) return `<div class="muted" style="font-size:12.5px">Read by ${esc(d.provider || "")} · ${esc(d.model)} from the ${esc(d.mode || "document")} · ${d.attempts} attempt(s).
      ${d.fallbacks && d.fallbacks.length ? `<div style="margin-top:4px">Backup used — first choice failed: ${esc(d.fallbacks.join(" · ").slice(0, 300))}</div>` : ""}
      Every field, with confidence and source text, is in the <b>Extracted data</b> tab.</div>`;
  if (k === "vendor" && d.vendor) return `<div class="muted" style="font-size:12.5px;margin-top:6px">Master record: GSTIN ${esc(d.vendor.gstin)} · ${esc(d.vendor.bank_name)} ${esc(d.vendor.bank_account)} · ${esc(d.vendor.registered_phone)}</div>`;
  if (k === "po" && d.po) return `<table class="mini-table"><tr><th>PO line</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">Amount</th></tr>
      ${d.po.lines.map((l) => `<tr><td>${esc(l.description)}</td><td class="num">${l.qty.toLocaleString("en-IN")}</td><td class="num">${inr(l.unit_price)}</td><td class="num">${inr(l.amount)}</td></tr>`).join("")}</table>`;
  if (k === "explain") return `<div class="muted" style="font-size:12.5px">The decision above was made by rules; this step only words it for the reader${d.explanation && d.explanation.source === "template" ? " (template used — AI unavailable)" : ""}.</div>`;
  return "";
}

function confHTML(c, min = 0.8) {
  if (c === undefined || c === null) return "";
  return `<span class="conf ${c < min ? "low" : ""}"><span class="b"><i style="width:${Math.round(c * 100)}%"></i></span>${Math.round(c * 100)}%</span>`;
}

function renderExtraction(ex, st) {
  if (!ex) return `<div class="empty">${st && st.status !== "pass" ? "The invoice could not be read." : "Waiting for the invoice to be read…"}</div>`;
  const rows = [["Vendor", "vendor_name"], ["Vendor GSTIN", "vendor_gstin"], ["Billed to", "buyer_name"], ["Billed-to GSTIN", "buyer_gstin"],
    ["Invoice number", "invoice_number"], ["Invoice date", "invoice_date"], ["Due date", "due_date"], ["PO reference", "po_reference"],
    ["Subtotal (pre-tax)", "subtotal", true], ["Tax treatment", "tax_treatment"], ["GST rate %", "tax_rate_percent"], ["Total", "total", true],
    ["Bank", "bank_name"], ["Account number", "bank_account"], ["IFSC", "ifsc"]];
  const field = ([label, k, money]) => {
    const f = ex[k] || {};
    const v = f.value === null || f.value === undefined || f.value === "" ? '<span class="muted">not on invoice</span>' : esc(money ? inr(f.value) : f.value);
    return `<tr><td class="k">${label}</td><td class="v">${v}${f.source_text && String(f.source_text) !== String(f.value) ? `<div class="src">“${esc(f.source_text)}”</div>` : ""}</td><td class="num">${f.value !== null && f.value !== undefined ? confHTML(f.confidence) : ""}</td></tr>`;
  };
  return `<h3>Header</h3><table class="fields">${rows.map(field).join("")}</table>
    <h3 style="margin-top:18px">Line items</h3>
    <table class="mini-table"><tr><th>Description</th><th class="num">Qty</th><th class="num">Rate</th><th class="num">Amount</th></tr>
      ${(ex.line_items || []).map((l) => `<tr><td>${esc(l.description)}</td><td class="num">${l.quantity ?? "—"}</td><td class="num">${inr(l.unit_price)}</td><td class="num">${inr(l.amount)}</td></tr>`).join("")}</table>
    ${(ex.tax_lines || []).length ? `<h3 style="margin-top:18px">Tax</h3><table class="mini-table">${ex.tax_lines.map((t) => `<tr><td>${esc(t.label)}</td><td class="num">${inr(t.amount)}</td></tr>`).join("")}</table>` : ""}
    ${ex.notes ? `<h3 style="margin-top:18px">Notes on invoice</h3><div>${esc(ex.notes)}</div>` : ""}`;
}

function verdictHTML(run) {
  const d = run.decision || "HOLD", e = run.explanation || {};
  const review = run.review_status;
  const word = { APPROVE: "APPROVED FOR PAYMENT", HOLD: "ON HOLD — NEEDS REVIEW", REJECT: "REJECTED" }[d];
  let reviewHTML = "";
  if (d === "HOLD" && review === "pending") {
    reviewHTML = `<div class="subtle-box review-box"><h3>Your decision</h3>
      <textarea id="note" placeholder="Why? This goes into the audit trail (required)"></textarea>
      <div style="display:flex;gap:8px"><button class="ok" id="approve">Approve for payment</button><button class="bad" id="reject">Reject</button></div></div>`;
  } else if (review && review !== "pending") {
    reviewHTML = `<div class="subtle-box"><h3>Reviewed</h3><span class="pill ${review}">${REVIEW_LABEL[review]}</span>
      <div style="margin-top:6px">${esc(run.reviewer_note)}</div><div class="muted" style="font-size:12px">${when(run.reviewed_at)}</div></div>`;
  }
  const steps = (e.next_steps || []).map((s) => `<li>${esc(s)}</li>`).join("");
  const email = e.vendor_email ? `<details class="subtle-box" style="margin-top:12px"><summary><b>Draft email to vendor</b> — ${esc(e.vendor_email.subject)}</summary>
      <div class="email" id="emailBody">${esc(e.vendor_email.body)}</div><button class="small" id="copyEmail" style="margin-top:8px">Copy email</button></details>` : "";
  return `<div class="verdict ${d}">
    <div class="word">${word}</div>
    <div class="headline">${esc(e.headline || run.headline || "")}</div>
    <div class="summary">${esc(e.summary || run.error || "")}</div>
    <div class="verdict-cols">
      <div>${steps ? `<h3>Next steps</h3><ol>${steps}</ol>` : ""}${email}</div>
      <div>${reviewHTML}</div>
    </div></div>`;
}

function bindReview(run, after) {
  const go = async (action) => {
    const note = $("#note").value.trim();
    if (!note) { toast("Add a note so the audit trail says why."); $("#note").focus(); return; }
    try { await api(`/api/runs/${run.id}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, note }) });
      toast(action === "approve" ? "Approved — posted to the ledger" : "Rejected"); after(); }
    catch (e) { toast(e.message); }
  };
  if ($("#approve")) { $("#approve").onclick = () => go("approve"); $("#reject").onclick = () => go("reject"); }
  if ($("#copyEmail")) $("#copyEmail").onclick = () => { navigator.clipboard.writeText($("#emailBody").textContent); toast("Email copied"); };
}

// ------------------------------------------------------------------ dashboard
async function viewDashboard() {
  let filter = "all";
  $("#view").innerHTML = `<div class="page-head"><div><h1>Dashboard</h1><div class="muted">Every invoice Checkpoint has processed, and what happened to it.</div></div>
    <a class="btn" href="#/">New run</a></div><div id="kpis"></div>
    <div class="dash-grid"><div class="card"><div class="pad" style="display:flex;justify-content:space-between;align-items:center;padding-bottom:12px">
      <h2 style="margin:0">Runs</h2><div class="filters" id="filters">
      ${["all", "APPROVE", "HOLD", "REJECT", "pending"].map((f) => `<button class="small ${f === "all" ? "on" : ""}" data-f="${f}">${{ all: "All", APPROVE: "Approved", HOLD: "Held", REJECT: "Rejected", pending: "Awaiting review" }[f]}</button>`).join("")}</div></div>
      <div id="runsTable"></div></div>
      <div class="card pad reasons" id="reasons"></div></div>`;
  const load = async () => {
    const [m, runs] = await Promise.all([api("/api/metrics"), api("/api/runs")]);
    const stp = m.straight_through_rate === null ? "—" : Math.round(m.straight_through_rate * 100) + "%";
    $("#kpis").innerHTML = `<div class="kpis">
      <div class="card kpi"><div class="l">Invoices processed</div><div class="v">${m.processed}</div><div class="s">${inr(m.value_processed)} total</div></div>
      <div class="card kpi"><div class="l">Straight-through</div><div class="v" style="color:var(--ok)">${stp}</div><div class="s">approved with no human touch</div></div>
      <div class="card kpi"><div class="l">Approved</div><div class="v">${m.approved}</div><div class="s">${inr(m.value_approved)}</div></div>
      <div class="card kpi"><div class="l">Held for review</div><div class="v" style="color:var(--hold)">${m.held}</div><div class="s">${m.pending_review} awaiting a decision</div></div>
      <div class="card kpi"><div class="l">Rejected</div><div class="v" style="color:var(--bad)">${m.rejected}</div><div class="s">${inr(m.value_rejected)} not paid</div></div>
      <div class="card kpi"><div class="l">Avg. time per invoice</div><div class="v">${m.avg_seconds ?? "—"}${m.avg_seconds ? "s" : ""}</div><div class="s">vs. ~10 min manually</div></div></div>`;
    const max = Math.max(1, ...m.reasons.map((r) => r[1]));
    $("#reasons").innerHTML = `<h2>Why invoices were stopped</h2>${m.reasons.length ? m.reasons.map(([t, n]) =>
      `<div class="r"><div style="display:flex;justify-content:space-between"><span>${esc(t)}</span><b>${n}</b></div><div class="bar"><i style="width:${100 * n / max}%"></i></div></div>`).join("") : '<div class="muted">Nothing stopped yet.</div>'}`;
    const shown = runs.filter((r) => filter === "all" || (filter === "pending" ? r.review_status === "pending" : r.decision === filter));
    $("#runsTable").innerHTML = shown.length ? `<table class="runs"><tr><th>#</th><th>When</th><th>Vendor · invoice</th><th class="num">Amount</th><th>PO</th><th>Decision</th><th>Why</th><th class="num">Time</th></tr>
      ${shown.map((r) => `<tr class="click" data-id="${r.id}"><td class="muted">${r.id}</td><td class="muted">${when(r.created_at)}</td>
        <td><b>${esc(r.vendor_name || r.filename)}</b><div class="muted mono">${esc(r.invoice_number || "no number")}</div></td>
        <td class="num">${inr(r.total)}</td><td class="mono">${esc(r.po_number || "—")}</td>
        <td>${r.status === "running" ? '<span class="pill running">Running</span>' : `<span class="pill ${r.decision}">${r.decision}</span>`}
          ${r.review_status ? `<div style="margin-top:4px"><span class="pill ${r.review_status}">${REVIEW_LABEL[r.review_status]}</span></div>` : ""}</td>
        <td class="why">${esc(r.top_reason || (r.decision === "APPROVE" ? "All checks passed" : ""))}</td>
        <td class="num muted">${r.duration_ms ? (r.duration_ms / 1000).toFixed(1) + "s" : ""}</td></tr>`).join("")}</table>`
      : `<div class="empty">No runs yet. <a href="#/">Check an invoice</a> to get started.</div>`;
    document.querySelectorAll("tr.click").forEach((tr) => (tr.onclick = () => (location.hash = `#/run/${tr.dataset.id}`)));
  };
  document.querySelectorAll("#filters button").forEach((b) => (b.onclick = () => {
    filter = b.dataset.f; document.querySelectorAll("#filters button").forEach((x) => x.classList.toggle("on", x === b)); load();
  }));
  await load();
  timers.push(setInterval(load, 5000));
}

// ------------------------------------------------------------------ review queue
async function viewReview() {
  const runs = (await api("/api/runs")).filter((r) => r.review_status === "pending");
  const full = await Promise.all(runs.map((r) => api(`/api/runs/${r.id}`)));
  $("#view").innerHTML = `<div class="page-head"><div><h1>Review queue</h1><div class="muted">Invoices Checkpoint stopped. Each one says what's wrong and what to do; your decision and note go into the audit trail.</div></div></div>
    <div class="queue">${full.length ? full.map((r) => `
      <div class="card q-item"><div>
        <div style="display:flex;gap:8px;align-items:center"><span class="pill HOLD">HOLD</span><a href="#/run/${r.id}"><b>${esc(r.vendor_name || r.filename)}</b> · ${esc(r.invoice_number || "no number")}</a><span class="muted">${inr(r.total)}</span></div>
        <div style="font-weight:600;margin-top:8px">${esc(r.headline)}</div>
        <ul class="reasons-list">${(r.findings || []).filter((f) => f.severity === "hold").map((f) => `<li>${esc(f.title)}${f.action ? ` — <span style="color:var(--ink)">${esc(f.action)}</span>` : ""}</li>`).join("")}</ul>
      </div><div class="review-box">
        <textarea data-note="${r.id}" placeholder="Note (required)"></textarea>
        <div style="display:flex;gap:8px"><button class="ok small" data-act="approve" data-id="${r.id}">Approve</button><button class="bad small" data-act="reject" data-id="${r.id}">Reject</button><a class="btn small" href="#/run/${r.id}">Open run</a></div>
      </div></div>`).join("") : '<div class="card empty">Nothing waiting for review.</div>'}</div>`;
  document.querySelectorAll("[data-act]").forEach((b) => (b.onclick = async () => {
    const note = $(`[data-note="${b.dataset.id}"]`).value.trim();
    if (!note) { toast("Add a note so the audit trail says why."); return; }
    try { await api(`/api/runs/${b.dataset.id}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: b.dataset.act, note }) });
      toast(b.dataset.act === "approve" ? "Approved — posted to the ledger" : "Rejected"); viewReview(); refreshBadge(); }
    catch (e) { toast(e.message); }
  }));
}

// ------------------------------------------------------------------ reference
async function viewReference(sub = "pos") {
  const ref = await api("/api/reference");
  const tabs = { pos: "Purchase orders", vendors: "Vendor master", ledger: "Invoice ledger" };
  let body = "";
  if (sub === "vendors") body = `<table class="runs"><tr><th>ID</th><th>Legal name</th><th>GSTIN</th><th>Bank account · IFSC</th><th>Registered phone</th><th>Status</th></tr>
    ${ref.vendors.map((v) => `<tr><td class="mono">${v.vendor_id}</td><td><b>${esc(v.legal_name)}</b><div class="muted">${esc(v.address)}</div></td><td class="mono">${v.gstin}</td>
      <td class="mono">${v.bank_account}<div class="muted">${v.ifsc} · ${esc(v.bank_name)}</div></td><td>${esc(v.registered_phone)}</td>
      <td>${v.status === "Active" ? '<span class="pill APPROVE">Active</span>' : `<span class="pill REJECT">${v.status}</span><div class="muted" style="font-size:12px">${esc(v.blocked_reason)}</div>`}</td></tr>`).join("")}</table>`;
  else if (sub === "ledger") body = `<table class="runs"><tr><th>Invoice</th><th>Vendor</th><th>PO</th><th>Date</th><th class="num">Pre-tax</th><th class="num">Total</th><th>Status</th><th>Source</th></tr>
    ${ref.ledger.map((l) => `<tr><td class="mono">${esc(l.invoice_number)}</td><td>${esc((ref.vendors.find((v) => v.vendor_id === l.vendor_id) || {}).legal_name)}</td><td class="mono">${esc(l.po_number)}</td>
      <td>${esc(l.invoice_date)}</td><td class="num">${inr(l.subtotal)}</td><td class="num">${inr(l.total)}</td><td>${esc(l.status)}${l.paid_on ? " · " + l.paid_on : ""}</td>
      <td>${l.source === "checkpoint" ? `<a href="#/run/${l.run_id}">Run #${l.run_id}</a>` : '<span class="muted">history</span>'}</td></tr>`).join("")}</table>`;
  else body = `<table class="runs"><tr><th>PO</th><th>Vendor</th><th>Lines</th><th class="num">Value (pre-tax)</th><th>Billed so far</th><th class="num">Remaining</th><th>Status</th></tr>
    ${ref.purchase_orders.map((p) => { const pct = Math.min(100, 100 * p.invoiced / p.subtotal); return `<tr><td class="mono">${p.po_number}<div class="muted">${p.po_date}</div></td><td>${esc(p.vendor_name)}</td>
      <td>${p.lines.map((l) => `<div>${esc(l.description)} <span class="muted">· ${l.qty.toLocaleString("en-IN")} × ${inr(l.unit_price)}</span></div>`).join("")}</td>
      <td class="num">${inr(p.subtotal)}<div class="muted">GST ${p.tax_rate}%</div></td>
      <td><span class="progress ${pct >= 100 ? "full" : ""}"><i style="width:${pct}%"></i></span> <span class="muted">${Math.round(pct)}%</span></td>
      <td class="num">${inr(p.remaining)}</td><td>${esc(p.status)}</td></tr>`; }).join("")}</table>`;
  $("#view").innerHTML = `<div class="page-head"><div><h1>Reference data</h1><div class="muted">What Checkpoint checks invoices against. Buyer: ${esc(ref.buyer.name)} · GSTIN ${ref.buyer.gstin}</div></div></div>
    <div class="card"><div class="tabs">${Object.entries(tabs).map(([k, t]) => `<button class="${k === sub ? "on" : ""}" onclick="location.hash='#/reference/${k}'">${t}</button>`).join("")}</div>${body}</div>`;
}

// ------------------------------------------------------------------ rules & assumptions
const SETTING_LABELS = {
  amount_tolerance_pct: ["Amount tolerance (%)", "Invoice may exceed the remaining PO balance by this %…"],
  amount_tolerance_abs: ["Amount tolerance (₹)", "…or this many rupees, whichever is lower"],
  price_tolerance_pct: ["Unit price tolerance (%)", "Max. a unit price may exceed the PO price"],
  auto_approve_limit: ["Auto-approval limit (₹)", "Above this, a finance manager must approve"],
  max_invoice_age_days: ["Max. invoice age (days)", "Older invoices are held"],
  min_field_confidence: ["Min. field confidence (0–1)", "Key fields read below this are not trusted"],
  po_inference_min_score: ["PO inference: min. score", "When an invoice has no PO number"],
  po_inference_min_gap: ["PO inference: min. lead", "Points the best PO must be ahead of the next"],
  duplicate_hold_score: ["Duplicate hold score", "Near-duplicate score (out of 110) that triggers a hold"],
};
async function viewRules() {
  const s = await api("/api/settings");
  $("#view").innerHTML = `<div class="page-head"><div><h1>Rules &amp; assumptions</h1><div class="muted">AI reads the invoice and words the result. Everything below decides — deterministically, so the same invoice always gets the same answer.</div></div></div>
    <div class="card pad"><h2>Business rules</h2><div class="settings">${Object.entries(SETTING_LABELS).map(([k, [l, h]]) =>
      `<label>${l}<small>${h}</small><input data-k="${k}" type="number" step="any" value="${s[k]}"></label>`).join("")}</div>
      <div style="margin-top:14px;display:flex;gap:8px"><button class="primary" id="save">Save rules</button><span class="muted" style="align-self:center">Applies to the next run.</span></div></div>
    <div class="card pad" style="margin-top:16px"><h2>How a decision is made</h2>
      <ol class="assumptions">
        <li><b>Read</b> (Gemini): every field with a confidence score and the exact text it came from.</li>
        <li><b>Validate</b>: required fields, line maths, subtotal + tax = total, GST type matches place of supply (CGST+SGST within Karnataka, IGST otherwise), GSTIN checksum, billed to Kestrel, dates.</li>
        <li><b>Vendor</b>: must be in the vendor master and active; bank account and IFSC must match the master.</li>
        <li><b>Duplicates</b>: exact number match against the ledger → reject. Otherwise a score from number core (+50), amount (+30), date (+15/20), PO (+10) → hold at ${s.duplicate_hold_score}+.</li>
        <li><b>PO</b>: use the printed PO; if none, score the vendor's open POs and accept only a clear winner.</li>
        <li><b>Amounts</b>: compared pre-tax against what is <i>left</i> on the PO after earlier invoices, plus unit prices and quantities per line.</li>
        <li><b>Decide</b>: worst finding wins — any reject → Reject, any hold → Hold, otherwise Approve. When unsure, hold.</li>
      </ol></div>
    <div class="card pad" style="margin-top:16px"><h2>Assumptions</h2><ul class="assumptions">
      <li>One buyer entity: Kestrel Foods Pvt Ltd, Bengaluru (Karnataka). Currency INR.</li>
      <li>2-way match (invoice ↔ PO). Goods-receipt (3-way) matching is the natural next step.</li>
      <li>Approved and reviewer-approved invoices are posted to the ledger, so a re-send is caught next time.</li>
      <li>Invoices arrive as PDF uploads; email intake would sit in front of step 1 in production.</li>
      <li>Vendor master, POs and history are sample data created for this exercise.</li></ul></div>
    <div class="card pad" style="margin-top:16px"><h2>Demo data</h2><p class="muted" style="margin-top:0">Clears all runs and restores the original ledger and rules.</p>
      <button class="bad" id="reset">Reset demo data</button></div>`;
  $("#save").onclick = async () => {
    const vals = {}; document.querySelectorAll("[data-k]").forEach((i) => (vals[i.dataset.k] = Number(i.value)));
    await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(vals) }); toast("Rules saved");
  };
  let armed = false;
  $("#reset").onclick = async () => {
    if (!armed) { armed = true; $("#reset").textContent = "Click again to confirm"; setTimeout(() => { armed = false; if ($("#reset")) $("#reset").textContent = "Reset demo data"; }, 4000); return; }
    await api("/api/reset", { method: "POST" }); toast("Demo data reset"); viewRules(); refreshBadge();
  };
}

health();
route();
