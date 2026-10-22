// Dashboard client. No build step, no external libraries.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDur = (s) => { s = Math.max(0, Math.round(s || 0)); const m = Math.floor(s / 60); return `${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };
const fmtDate = (iso) => iso ? new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) : "";
const ICON = {
  incoming: '<svg viewBox="0 0 24 24"><path d="M16 3v5h5M21 3l-6 6M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2"/></svg>',
  outgoing: '<svg viewBox="0 0 24 24"><path d="M15 3h6v6M21 3l-7 7M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3"/></svg>',
  refresh: '<svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5"/></svg>',
};

let toastTimer;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (bad ? " bad" : ""); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), bad ? 6000 : 2500);
}

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body });
  if (res.status === 401) { location.href = "/login"; throw new Error("Please log in"); }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const d = data?.detail;
    const msg = Array.isArray(d) ? d.map((e) => `${e.loc?.slice(-1)[0]}: ${e.msg}`).join("; ") : d || `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}
const safe = (fn) => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, true); } };

// ------------------------------------------------------------------ tabs
const TITLES = { live: "Live", history: "Call history", memory: "Memory & contacts", analytics: "Analytics", settings: "Settings" };
const loaders = {};
function showTab(name) {
  $$(".nav").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${name}`));
  $("#pageTitle").textContent = TITLES[name];
  history.replaceState(null, "", `#${name}`);
  loaders[name]?.();
}
$$(".nav").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ------------------------------------------------------------------ live calls
const ACTIVE = ["starting", "connecting", "live", "reconnecting", "ending"];
const STATE_LABEL = { idle: "Ready", connecting: "Connecting", live: "Live", reconnecting: "Reconnecting", ending: "Saving", error: "Error", starting: "Starting" };
const calls = new Map();   // key -> {key, call_id, state, direction, mode, contact_name, contact_phone, startedAt, segs: Map, interrupts}
let selected = null;       // key of the call shown in the transcript
let status = {};

function upsertCall(ev) {
  let c = calls.get(ev.key);
  if (!c) { c = { key: ev.key, segs: new Map(), interrupts: 0, startedAt: Date.now() - (ev.elapsed_s || 0) * 1000 }; calls.set(ev.key, c); }
  for (const k of ["call_id", "state", "direction", "mode", "contact_name", "contact_phone"]) if (ev[k] !== undefined && ev[k] !== null) c[k] = ev[k];
  if (!selected || !calls.get(selected) || !ACTIVE.includes(calls.get(selected).state)) select(ev.key);
  return c;
}
const callLabel = (c) => c.contact_name || c.contact_phone || (c.mode === "test" ? "Practice call" : "Unknown caller");

function renderActive() {
  const active = [...calls.values()].filter((c) => ACTIVE.includes(c.state));
  $("#activeCount").textContent = active.length ? `(${active.length})` : "";
  const list = $("#activeList");
  if (!active.length) {
    const phoneReady = status.vobiz_enabled;
    list.innerHTML = `<div class="ready ${phoneReady ? "" : "off"}"><i></i><div><strong>${phoneReady ? "Ready for calls" : "Phone line not connected"}</strong>
      <small>${phoneReady ? `Calls forwarded to ${esc(status.vobiz_number)} are answered automatically.` : "Set up Vobiz in .env (see Settings → Phone line). Practice calls still work."}</small></div></div>`;
  } else {
    list.innerHTML = active.map((c) => `
      <button class="call-chip ${c.key === selected ? "sel" : ""}" data-key="${esc(c.key)}">
        <div class="dir-ic ${esc(c.direction)}">${ICON[c.direction] || ICON.incoming}</div>
        <div style="min-width:0"><strong>${esc(callLabel(c))}</strong><small>${esc(STATE_LABEL[c.state] || c.state)}${c.contact_name && c.contact_phone ? " · " + esc(c.contact_phone) : ""}</small></div>
        <span class="t" data-timer="${esc(c.key)}">${fmtDur((Date.now() - c.startedAt) / 1000)}</span>
      </button>`).join("");
    $$(".call-chip", list).forEach((b) => b.addEventListener("click", () => select(b.dataset.key)));
  }
  const sel = calls.get(selected);
  const selActive = sel && ACTIVE.includes(sel.state);
  $("#stopBtn").hidden = !selActive;
  $("#stopBtn").disabled = sel?.state === "ending";
  const b = $("#statusBadge"), st = sel ? sel.state : "idle";
  b.dataset.state = selActive || st === "error" ? st : "idle";
  $("span", b).textContent = selActive || st === "error" ? STATE_LABEL[st] || st : (active.length ? "Live" : "Ready");
  $("#timer").hidden = !selActive;
}
setInterval(() => {
  $$("[data-timer]").forEach((el) => { const c = calls.get(el.dataset.timer); if (c) el.textContent = fmtDur((Date.now() - c.startedAt) / 1000); });
  const sel = calls.get(selected);
  if (sel && ACTIVE.includes(sel.state)) $("#timer").textContent = fmtDur((Date.now() - sel.startedAt) / 1000);
}, 500);

function select(key) {
  selected = key;
  const c = calls.get(key);
  const box = $("#liveTranscript");
  box.innerHTML = "";
  if (c) {
    [...c.segs.values()].sort((a, b) => a.start_ms - b.start_ms || a.seq - b.seq).forEach((s) => renderSeg(c, s));
    $("#liveCallId").textContent = `${callLabel(c)}${c.call_id ? " · #" + c.call_id : ""}`;
    $("#interrupts").textContent = String(c.interrupts);
    $("#latency").textContent = c.latency ? `${c.latency} ms` : "—";
    $("#devLine").innerHTML = c.devices || "";
  }
  if (!box.children.length) box.innerHTML = `<div class="empty"><p>${c ? "Waiting for the first words…" : "When a call comes in, the conversation appears here in real time."}</p></div>`;
  renderActive();
}

function renderSeg(c, seg) {
  if (c.key !== selected) return;
  const box = $("#liveTranscript");
  $(".empty", box)?.remove();
  let el = box.querySelector(`[data-seq="${seg.seq}"]`);
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  if (!el) {
    el = document.createElement("div");
    el.className = `msg ${seg.speaker}`; el.dataset.seq = seg.seq; el.dataset.start = seg.start_ms;
    el.innerHTML = `<div class="who"></div><div class="txt"></div>`;
    const after = [...box.children].find((x) => Number(x.dataset.start) > seg.start_ms);  // caller text can arrive late
    after ? box.insertBefore(el, after) : box.appendChild(el);
  }
  el.classList.toggle("partial", !seg.final);
  const who = seg.speaker === "caller" ? callLabel(c) : "Assistant";
  $(".who", el).innerHTML = `<span>${esc(who)}</span><span>${fmtDur(seg.start_ms / 1000)}</span>${seg.interrupted ? '<span class="tag warn">interrupted</span>' : ""}`;
  $(".txt", el).textContent = seg.text;
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

// ------------------------------------------------------------------ websocket
let ws, wsRetry = 0;
function connectWS() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { wsRetry = 0; $("#wsDot").classList.add("on"); };
  ws.onclose = (e) => {
    $("#wsDot").classList.remove("on");
    if (e.code === 1008) { location.href = "/login"; return; }
    setTimeout(connectWS, Math.min(5000, 300 * 2 ** wsRetry++));
  };
  ws.onmessage = (m) => handleEvent(JSON.parse(m.data));
}
function handleEvent(ev) {
  const c = ev.key ? calls.get(ev.key) : null;
  switch (ev.type) {
    case "snapshot":
      calls.clear(); selected = null;
      (ev.calls || []).forEach((sc) => { const cc = upsertCall(sc); (sc.segments || []).forEach((s) => cc.segs.set(s.seq, s)); });
      selected ? select(selected) : renderActive();
      break;
    case "status": {
      if (!ev.key) { if (ev.state === "error") toast(ev.message, true); break; }
      const cc = upsertCall(ev);
      if (ev.state === "error" && ev.message) { $("#banner").textContent = ev.message; $("#banner").hidden = false; }
      if (ev.state === "connecting" && cc.segs.size === 0) $("#banner").hidden = true;
      if (ev.state === "reconnecting") toast(ev.message || "Reconnecting…");
      if (cc.key === selected) $("#liveCallId").textContent = `${callLabel(cc)}${cc.call_id ? " · #" + cc.call_id : ""}`;
      renderActive();
      break;
    }
    case "segment": if (c) { c.segs.set(ev.seq, ev); renderSeg(c, ev); } break;
    case "levels":
      if (ev.key === selected) {
        $("#mCaller").style.width = `${Math.round(Math.sqrt(ev.caller) * 100)}%`;
        $("#mAi").style.width = `${Math.round(Math.sqrt(ev.ai) * 100)}%`;
        $("#bufferMs").textContent = `${ev.buffer_ms} ms`;
      }
      break;
    case "latency": if (c) { c.latency = ev.ms; if (ev.key === selected) $("#latency").textContent = `${ev.ms} ms`; } break;
    case "interrupted": if (c) { c.interrupts++; if (ev.key === selected) $("#interrupts").textContent = String(c.interrupts); } break;
    case "devices": if (c) { c.devices = `🎙 ${esc(ev.input)}<br>🔊 ${esc(ev.output)}`; if (ev.key === selected) $("#devLine").innerHTML = c.devices; } break;
    case "call_saved": toast(`Call #${ev.call_id} saved. Summary is being generated…`); break;
    case "call_removed":
      if (c) { c.state = "idle"; }
      renderActive();
      $("#mCaller").style.width = "0"; $("#mAi").style.width = "0";
      break;
    case "dialing": toast(`Dialing ${ev.contact_name || ev.to}…`); break;
  }
}

// ------------------------------------------------------------------ live controls
function seg(id) {
  const el = $(id);
  el.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    $$("button", el).forEach((x) => x.classList.toggle("on", x === b));
    el.dispatchEvent(new Event("change"));
  });
  return () => $("button.on", el).dataset.v;
}
const getDir = seg("#dirSeg");
$("#dirSeg").addEventListener("change", () => ($("#purposeWrap").hidden = getDir() !== "outgoing"));

$("#testBtn").addEventListener("click", safe(async () => {
  $("#testBtn").disabled = true;
  try {
    await api("/api/call/test", { method: "POST", body: { direction: getDir(), contact_name: $("#testName").value.trim(), purpose: $("#testPurpose").value.trim() } });
  } finally { $("#testBtn").disabled = false; }
}));
$("#dialBtn").addEventListener("click", safe(async () => {
  const to = $("#outPhone").value.trim(), purpose = $("#outPurpose").value.trim();
  if (!to) { $("#outPhone").focus(); throw new Error("Enter the phone number to call."); }
  if (purpose.length < 3) { $("#outPurpose").focus(); throw new Error("Tell the assistant what to do on the call."); }
  $("#dialBtn").disabled = true;
  try { await api("/api/call/outbound", { method: "POST", body: { to, contact_name: $("#outName").value.trim(), purpose } }); }
  finally { $("#dialBtn").disabled = false; }
}));
$("#stopBtn").addEventListener("click", safe(async () => {
  if (!selected) return;
  await api(`/api/calls/live/${encodeURIComponent(selected)}/stop`, { method: "POST" });
}));
$("#logoutBtn").addEventListener("click", safe(async () => { await api("/api/logout", { method: "POST" }); location.href = "/login"; }));

// ------------------------------------------------------------------ history
let selectedCall = null, searchTimer, pollTimer;
loaders.history = safe(async () => {
  const q = $("#search").value.trim();
  const rows = await api(`/api/calls?q=${encodeURIComponent(q)}&limit=100`);
  const list = $("#callList");
  if (!rows.length) { list.innerHTML = `<div class="empty" style="padding:30px 0"><p>${q ? "No calls match your search." : "No calls yet."}</p></div>`; return; }
  list.innerHTML = rows.map((c) => `
    <button class="call-item ${c.id === selectedCall ? "sel" : ""}" data-id="${c.id}">
      <div class="dir-ic ${c.direction}">${ICON[c.direction]}</div>
      <div style="min-width:0"><div class="ci-title">${esc(c.title || c.contact_name || c.contact_phone || "Call #" + c.id)}</div>
        <div class="ci-sub">${esc(c.contact_name || c.contact_phone || "Unknown")} · ${c.mode === "test" ? "practice · " : ""}${fmtDate(c.started_at)}</div></div>
      <div class="ci-right">${fmtDur(c.duration_s)}<br><span class="pill ${c.status}">${c.status}</span></div>
      ${c.snippet ? `<div class="ci-snip">${esc(c.snippet).replace(/&lt;(\/?)mark&gt;/g, "<$1mark>")}</div>` : ""}
    </button>`).join("");
  $$(".call-item", list).forEach((b) => b.addEventListener("click", () => openCall(Number(b.dataset.id))));
});
$("#search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loaders.history, 250); });

const listSec = (title, items, fmt = (x) => esc(x)) =>
  items?.length ? `<div class="sec"><h4>${title}</h4><ul>${items.map((x) => `<li>${fmt(x)}</li>`).join("")}</ul></div>` : "";

function transcriptHTML(segs, who) {
  if (!segs?.length) return `<div class="empty"><p>No transcript.</p></div>`;
  return segs.map((s) => `<div class="msg ${esc(s.speaker)}"><div class="who"><span>${esc(s.speaker === "caller" ? who : "Assistant")}</span>${s.start_ms ? `<span>${fmtDur(s.start_ms / 1000)}</span>` : ""}${s.interrupted ? '<span class="tag warn">interrupted</span>' : ""}</div><div class="txt">${esc(s.text)}</div></div>`).join("");
}

const openCall = safe(async (id) => {
  selectedCall = id;
  $$(".call-item").forEach((b) => b.classList.toggle("sel", Number(b.dataset.id) === id));
  const c = await api(`/api/calls/${id}`);
  const a = c.analysis || {};
  const el = $("#callDetail");
  const pending = ["queued", "processing", "live"].includes(c.status);
  const who = c.contact_name || c.contact_phone || "Caller";
  el.innerHTML = `
    <div class="d-head">
      <div><h2>${esc(c.title || who)}</h2>
        <div class="d-meta"><span>${c.direction === "incoming" ? "Incoming" : "Outgoing"}${c.mode === "test" ? " (practice)" : ""}</span>
          <span>${esc(c.contact_name || "Unknown")}${c.contact_phone ? " · " + esc(c.contact_phone) : ""}</span>
          <span>${fmtDate(c.started_at)}</span><span>${fmtDur(c.duration_s)}</span>
          ${c.sentiment ? `<span>Mood: ${esc(c.sentiment)}</span>` : ""}<span class="pill ${c.status}">${c.status}</span></div></div>
      <div class="d-actions">
        <button class="btn sm ghost" id="dReprocess" title="Run summary again">${ICON.refresh}Re-run</button>
        <button class="btn sm ghost" id="dDelete" title="Delete call">${ICON.trash}</button>
      </div>
    </div>
    ${c.contact_phone && !c.contact_name ? `<form class="contact-form" id="nameForm"><input id="nameInput" maxlength="200" placeholder="Who is ${esc(c.contact_phone)}? Save a name" required><span></span><button class="btn sm">Save name</button></form>` : ""}
    ${c.purpose ? `<div class="sec" style="margin-bottom:12px"><h4>Purpose</h4>${esc(c.purpose)}</div>` : ""}
    ${pending ? `<div class="processing-note"><div class="spinner"></div>Summary is being generated… this refreshes automatically.</div>` : ""}
    ${c.error ? `<div class="err">${esc(c.error)}</div>` : ""}
    ${c.has_audio ? `<audio controls preload="metadata" src="/api/calls/${c.id}/audio"></audio>` : ""}
    ${c.summary ? `<div class="summary">${esc(c.summary)}</div>` : ""}
    <div class="sections">
      ${listSec("Key points", a.key_points)}
      ${listSec("Decisions", a.decisions)}
      ${listSec("Promises", a.promises, (p) => `${esc(p.by)} → ${esc(p.to)}: ${esc(p.what)}${p.due ? ` <small>(${esc(p.due)})</small>` : ""}`)}
      ${c.action_items?.length ? `<div class="sec"><h4>Action items</h4>${c.action_items.map((i) => `
        <label class="check ${i.done ? "done" : ""}"><input type="checkbox" data-aid="${i.id}" ${i.done ? "checked" : ""}>
        <span>${esc(i.task)} <small style="color:var(--muted)">${esc(i.owner)}${i.due ? " · " + esc(i.due) : ""}</small></span></label>`).join("")}</div>` : ""}
      ${listSec("Follow-ups", a.follow_ups)}
      ${listSec("New memory", a.memory_facts, (f) => esc(f.fact))}
    </div>
    <div class="subtabs"><button class="on" data-t="corrected">Transcript</button><button data-t="live">Live (raw)</button></div>
    <div class="d-transcript" id="dTranscript"></div>`;
  const tx = { corrected: c.corrected_transcript?.length ? c.corrected_transcript : c.live_transcript, live: c.live_transcript };
  const showTx = (k) => { $("#dTranscript").innerHTML = transcriptHTML(tx[k], who); $$(".subtabs button", el).forEach((b) => b.classList.toggle("on", b.dataset.t === k)); };
  showTx("corrected");
  $$(".subtabs button", el).forEach((b) => b.addEventListener("click", () => showTx(b.dataset.t)));
  $$("input[data-aid]", el).forEach((cb) => cb.addEventListener("change", safe(async () => {
    await api(`/api/actions/${cb.dataset.aid}`, { method: "PATCH", body: { done: cb.checked } });
    cb.closest(".check").classList.toggle("done", cb.checked);
  })));
  $("#nameForm")?.addEventListener("submit", safe(async (e) => {
    e.preventDefault();
    await api("/api/contacts", { method: "PUT", body: { phone: c.contact_phone, name: $("#nameInput").value.trim() } });
    toast("Contact saved"); openCall(id); loaders.history();
  }));
  $("#dReprocess").addEventListener("click", safe(async () => { await api(`/api/calls/${id}/reprocess`, { method: "POST" }); toast("Re-processing queued"); openCall(id); }));
  const del = $("#dDelete");
  del.addEventListener("click", safe(async () => {
    if (!del.dataset.armed) { del.dataset.armed = "1"; del.innerHTML = `${ICON.trash}Confirm delete`; del.classList.add("danger"); setTimeout(() => { if (del.isConnected) { delete del.dataset.armed; del.innerHTML = ICON.trash; del.classList.remove("danger"); } }, 3000); return; }
    await api(`/api/calls/${id}`, { method: "DELETE" });
    selectedCall = null; el.innerHTML = `<div class="empty"><p>Call deleted.</p></div>`; loaders.history(); toast("Call deleted");
  }));
  clearTimeout(pollTimer);
  if (pending) pollTimer = setTimeout(() => { if (selectedCall === id && $("#tab-history").classList.contains("active")) { openCall(id); loaders.history(); } }, 4000);
});

// ------------------------------------------------------------------ memory + contacts
loaders.memory = safe(async () => {
  const [facts, contacts] = await Promise.all([api("/api/memory"), api("/api/contacts")]);
  $("#factCount").textContent = facts.length ? `(${facts.length})` : "";
  $("#factList").innerHTML = facts.length ? facts.map((f) => `
    <div class="fact"><p>${esc(f.fact)}</p>${f.contact_name || f.contact_phone ? `<span class="who-tag">${esc(f.contact_name || f.contact_phone)}</span>` : ""}
      ${f.source_call_id ? `<span class="pill">auto</span>` : ""}
      <button class="icon-btn" data-fid="${f.id}" title="Delete">${ICON.trash}</button></div>`).join("")
    : `<div class="empty" style="padding:24px 0"><p>No facts yet. They'll appear after your first summarized call.</p></div>`;
  $$("[data-fid]").forEach((b) => b.addEventListener("click", safe(async () => { await api(`/api/memory/${b.dataset.fid}`, { method: "DELETE" }); loaders.memory(); })));
  $("#contactCount").textContent = contacts.length ? `(${contacts.length})` : "";
  $("#contactList").innerHTML = contacts.length ? contacts.map((c) => `
    <div class="fact"><p>${esc(c.name)} <span class="ph">${esc(c.phone)}</span></p>${c.source === "auto" ? `<span class="pill">auto</span>` : ""}
      <small class="muted">${c.calls} call${c.calls === 1 ? "" : "s"}</small>
      <button class="icon-btn" data-cph="${esc(c.phone)}" title="Delete">${ICON.trash}</button></div>`).join("")
    : `<div class="empty" style="padding:16px 0"><p>No contacts yet.</p></div>`;
  $$("[data-cph]").forEach((b) => b.addEventListener("click", safe(async () => { await api(`/api/contacts/${encodeURIComponent(b.dataset.cph)}`, { method: "DELETE" }); loaders.memory(); })));
});
$("#factForm").addEventListener("submit", safe(async (e) => {
  e.preventDefault();
  await api("/api/memory", { method: "POST", body: { fact: $("#factText").value.trim(), contact_name: $("#factContact").value.trim() } });
  $("#factText").value = ""; $("#factContact").value = ""; toast("Fact added"); loaders.memory();
}));
$("#contactForm").addEventListener("submit", safe(async (e) => {
  e.preventDefault();
  await api("/api/contacts", { method: "PUT", body: { phone: $("#cPhone").value.trim(), name: $("#cName").value.trim() } });
  $("#cPhone").value = ""; $("#cName").value = ""; toast("Contact saved"); loaders.memory();
}));

// ------------------------------------------------------------------ analytics
function barChart(el, data) {
  const W = el.clientWidth || 600, H = el.clientHeight || 260, pad = { l: 28, r: 6, t: 10, b: 26 };
  const max = Math.max(1, ...data.map((d) => d.calls));
  const step = max <= 5 ? 1 : Math.ceil(max / 4);
  const top = Math.ceil(max / step) * step;
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b, slot = iw / data.length, bw = Math.max(4, Math.min(28, slot - 6));
  const y = (v) => pad.t + ih - (v / top) * ih;
  let g = '<g class="grid">', ax = '<g class="axis">';
  for (let v = 0; v <= top; v += step) { g += `<line x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}"/>`; ax += `<text x="${pad.l - 8}" y="${y(v) + 4}" text-anchor="end">${v}</text>`; }
  let bars = "";
  data.forEach((d, i) => {
    const cx = pad.l + slot * i + slot / 2, h = ih - (y(d.calls) - pad.t), x = cx - bw / 2;
    const r = Math.min(4, bw / 2, h);
    const path = h > 0 ? `M${x},${y(0)} V${y(d.calls) + r} Q${x},${y(d.calls)} ${x + r},${y(d.calls)} H${x + bw - r} Q${x + bw},${y(d.calls)} ${x + bw},${y(d.calls) + r} V${y(0)} Z` : "";
    const label = new Date(d.day + "T00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" });
    bars += `<g class="b" data-i="${i}"><rect class="hit" x="${pad.l + slot * i}" y="${pad.t}" width="${slot}" height="${ih}"/>${path ? `<path class="bar-rect" d="${path}"/>` : ""}</g>`;
    if (i % 2 === (data.length - 1) % 2) ax += `<text x="${cx}" y="${H - 6}" text-anchor="middle">${esc(label)}</text>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Calls per day, last 14 days">${g}</g>${bars}${ax}</g></svg><div class="chart-tip" hidden></div>`;
  const tip = $(".chart-tip", el);
  $$("g.b", el).forEach((b) => {
    b.addEventListener("mouseenter", () => {
      const d = data[Number(b.dataset.i)], cx = pad.l + slot * Number(b.dataset.i) + slot / 2;
      tip.innerHTML = `<b>${d.calls} call${d.calls === 1 ? "" : "s"}</b>${new Date(d.day + "T00:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })} · ${d.minutes} min`;
      tip.style.left = `${(cx / W) * 100}%`; tip.style.top = `${(y(d.calls) / H) * 100}%`; tip.hidden = false;
    });
    b.addEventListener("mouseleave", () => (tip.hidden = true));
  });
}
let lastPerDay = null;
loaders.analytics = safe(async () => {
  const [a, actions] = await Promise.all([api("/api/analytics"), api("/api/actions")]);
  $("#tiles").innerHTML = [
    ["Total calls", a.total_calls, ""], ["Talk time", a.total_minutes, "min"],
    ["Average length", fmtDur(a.avg_seconds), ""], ["Open action items", a.open_action_items, ""],
  ].map(([l, v, u]) => `<div class="tile"><small>${l}</small><strong>${esc(v)}<span>${u}</span></strong></div>`).join("");
  lastPerDay = a.per_day; barChart($("#chart"), a.per_day);
  $("#actionList").innerHTML = actions.length ? actions.map((i) => `
    <div class="action"><label class="check"><input type="checkbox" data-aid="${i.id}"><span>${esc(i.task)}</span></label>
    <small>${esc(i.owner || "—")}${i.due ? " · due " + esc(i.due) : ""} · ${esc(i.contact_name || "call #" + i.call_id)}</small></div>`).join("")
    : `<div class="empty" style="padding:24px 0"><p>Nothing open. 🎉</p></div>`;
  $$("#actionList input[data-aid]").forEach((cb) => cb.addEventListener("change", safe(async () => {
    await api(`/api/actions/${cb.dataset.aid}`, { method: "PATCH", body: { done: cb.checked } }); setTimeout(loaders.analytics, 300);
  })));
});
window.addEventListener("resize", () => { if (lastPerDay && $("#tab-analytics").classList.contains("active")) barChart($("#chart"), lastPerDay); });

// ------------------------------------------------------------------ settings
loaders.settings = safe(async () => {
  const [{ values, voices }, tel] = await Promise.all([api("/api/settings"), api("/api/telephony")]);
  $("#styleRules").value = values.style_rules;
  $("#voice").innerHTML = Object.entries(voices).map(([n, d]) => `<option value="${esc(n)}" ${n === values.voice ? "selected" : ""}>${esc(n)} — ${esc(d)}</option>`).join("");
  $("#recording").checked = !!values.recording_enabled;
  $("#silence").value = values.silence_timeout_s;
  const rows = [
    ["Status", tel.enabled ? `<span class="ok">Connected</span>` : `<span class="bad">Not configured${tel.missing.length ? ": missing " + esc(tel.missing.join(", ")) : ""}</span>`],
    ["Assistant number", esc(tel.number || "—")], ["Your mobile", esc(tel.owner_phone || "—")],
  ];
  if (tel.answer_url) rows.push(["Vobiz Answer URL (paste in Vobiz application)", `<div class="url-box" title="Click to copy" data-copy="${esc(tel.answer_url)}">${esc(tel.answer_url)}</div>`],
    ["Vobiz Hangup URL", `<div class="url-box" title="Click to copy" data-copy="${esc(tel.hangup_url)}">${esc(tel.hangup_url)}</div>`]);
  $("#telephony").innerHTML = rows.map(([k, v]) => `<div><small>${k}</small><span>${v}</span></div>`).join("");
  $$("[data-copy]").forEach((el) => el.addEventListener("click", async () => { try { await navigator.clipboard.writeText(el.dataset.copy); toast("Copied"); } catch { /* clipboard blocked */ } }));
});
$("#saveSettings").addEventListener("click", safe(async () => {
  await api("/api/settings", { method: "PUT", body: {
    style_rules: $("#styleRules").value, voice: $("#voice").value,
    recording_enabled: $("#recording").checked, silence_timeout_s: Number($("#silence").value) } });
  toast("Settings saved. They apply from the next call.");
}));

// ------------------------------------------------------------------ boot
(async () => {
  try {
    status = await api("/api/status");
    $("#ownerLabel").textContent = `${status.owner_name}'s assistant`;
    $("#modelChip").textContent = status.live_model;
    $("#outboundCard").hidden = !status.vobiz_enabled;
    $("#vobizNumber").textContent = status.vobiz_number ? `(${status.vobiz_number})` : "";
    $("#testCard").hidden = !status.local_audio;
    $("#logoutBtn").hidden = !status.auth;
    renderActive();
  } catch (e) { toast(e.message, true); }
  connectWS();
  const initial = location.hash.slice(1);
  showTab(TITLES[initial] ? initial : "live");
})();
