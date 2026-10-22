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
  download: '<svg viewBox="0 0 24 24"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>',
};

let toastTimer;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (bad ? " bad" : ""); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), bad ? 6000 : 2500);
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body });
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
const TITLES = { live: "Live", history: "Call history", memory: "Memory", analytics: "Analytics", settings: "Settings" };
const loaders = {};
function showTab(name) {
  $$(".nav").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${name}`));
  $("#pageTitle").textContent = TITLES[name];
  history.replaceState(null, "", `#${name}`);
  loaders[name]?.();
}
$$(".nav").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ------------------------------------------------------------------ live state
const live = { state: "idle", callId: null, startedAt: null, segs: new Map(), interrupts: 0 };
const STATE_LABEL = { idle: "Idle", connecting: "Connecting", live: "Live", reconnecting: "Reconnecting", ending: "Saving", error: "Error" };

function setState(state, message = "") {
  live.state = state;
  const b = $("#statusBadge");
  b.dataset.state = state; $("span", b).textContent = STATE_LABEL[state] || state;
  const active = ["connecting", "live", "reconnecting", "ending"].includes(state);
  $("#startBtn").hidden = active; $("#stopBtn").hidden = !active;
  $("#stopBtn").disabled = state === "ending";
  ["#modeSeg", "#dirSeg"].forEach((s) => $(s).classList.toggle("locked", active));
  $$("#contactName, #contactPhone, #purpose").forEach((el) => (el.disabled = active));
  $("#timer").hidden = !active;
  if (active && !live.startedAt) live.startedAt = Date.now();
  if (!active) live.startedAt = null;
  const banner = $("#banner");
  if (state === "error" && message) { banner.textContent = message; banner.hidden = false; }
  else if (active) banner.hidden = true;
}
setInterval(() => { if (live.startedAt) $("#timer").textContent = fmtDur((Date.now() - live.startedAt) / 1000); }, 500);

function segEl(seg) {
  const div = document.createElement("div");
  div.className = `msg ${seg.speaker}`;
  div.innerHTML = `<div class="who"></div><div class="txt"></div>`;
  return div;
}
function renderSeg(seg) {
  const box = $("#liveTranscript");
  $(".empty", box)?.remove();
  let el = live.segs.get(seg.seq);
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  if (!el) {
    el = segEl(seg); live.segs.set(seg.seq, el);
    el.dataset.start = seg.start_ms;
    // insert ordered by speech start time (caller transcription can arrive late)
    const after = [...box.children].find((c) => Number(c.dataset.start) > seg.start_ms);
    after ? box.insertBefore(el, after) : box.appendChild(el);
  }
  el.classList.toggle("partial", !seg.final);
  const who = seg.speaker === "caller" ? (live.contact || "Caller") : "Assistant";
  $(".who", el).innerHTML = `<span>${esc(who)}</span><span>${fmtDur(seg.start_ms / 1000)}</span>${seg.interrupted ? '<span class="tag warn">interrupted</span>' : ""}`;
  $(".txt", el).textContent = seg.text;
  if (nearBottom) box.scrollTop = box.scrollHeight;
}
function resetTranscript() {
  live.segs.clear(); live.interrupts = 0;
  $("#interrupts").textContent = "0"; $("#latency").textContent = "—";
  $("#liveTranscript").innerHTML = "";
}

// ------------------------------------------------------------------ websocket
let ws, wsRetry = 0;
function connectWS() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { wsRetry = 0; $("#wsDot").classList.add("on"); };
  ws.onclose = () => { $("#wsDot").classList.remove("on"); setTimeout(connectWS, Math.min(5000, 300 * 2 ** wsRetry++)); };
  ws.onmessage = (m) => handleEvent(JSON.parse(m.data));
}
function handleEvent(ev) {
  switch (ev.type) {
    case "snapshot":
      if (ev.active) {
        resetTranscript();
        live.callId = ev.call_id; live.contact = ev.contact_name;
        live.startedAt = Date.now() - (ev.elapsed_s || 0) * 1000;
        (ev.segments || []).forEach(renderSeg);
        $("#liveCallId").textContent = ev.call_id ? `#${ev.call_id}` : "";
      }
      setState(ev.state || "idle", ev.message);
      break;
    case "status":
      if (ev.state === "connecting" && live.state !== "connecting" && live.state !== "live") { resetTranscript(); live.startedAt = Date.now(); }
      if (ev.call_id) { live.callId = ev.call_id; $("#liveCallId").textContent = `#${ev.call_id}`; }
      if (ev.contact_name !== undefined) live.contact = ev.contact_name;
      setState(ev.state, ev.message);
      if (ev.state === "reconnecting") toast(ev.message || "Reconnecting…");
      break;
    case "segment": renderSeg(ev); break;
    case "levels":
      $("#mCaller").style.width = `${Math.round(Math.sqrt(ev.caller) * 100)}%`;
      $("#mAi").style.width = `${Math.round(Math.sqrt(ev.ai) * 100)}%`;
      $("#bufferMs").textContent = `${ev.buffer_ms} ms`;
      break;
    case "latency": $("#latency").textContent = `${ev.ms} ms`; break;
    case "interrupted": $("#interrupts").textContent = String(++live.interrupts); break;
    case "devices": $("#devLine").innerHTML = `🎙 ${esc(ev.input)}<br>🔊 ${esc(ev.output)}`; break;
    case "call_saved": toast(`Call #${ev.call_id} saved. Summary is being generated…`); break;
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
const getMode = seg("#modeSeg"), getDir = seg("#dirSeg");
$("#dirSeg").addEventListener("change", () => ($("#purposeWrap").hidden = getDir() !== "outgoing"));

$("#startBtn").addEventListener("click", safe(async () => {
  if (getDir() === "outgoing" && !$("#purpose").value.trim()) { $("#purpose").focus(); throw new Error("Add the call purpose for an outgoing call."); }
  $("#startBtn").disabled = true;
  try {
    resetTranscript(); live.contact = $("#contactName").value.trim();
    await api("/api/call/start", { method: "POST", body: {
      mode: getMode(), direction: getDir(), contact_name: live.contact,
      contact_phone: $("#contactPhone").value.trim(), purpose: $("#purpose").value.trim() } });
    setState("connecting");
  } finally { $("#startBtn").disabled = false; }
}));
$("#stopBtn").addEventListener("click", safe(async () => { await api("/api/call/stop", { method: "POST" }); setState("ending"); }));

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
      <div style="min-width:0"><div class="ci-title">${esc(c.title || c.contact_name || (c.status === "done" ? "Untitled call" : "Call #" + c.id))}</div>
        <div class="ci-sub">${esc(c.contact_name || "Unknown")} · ${c.mode === "test" ? "test · " : ""}${fmtDate(c.started_at)}</div></div>
      <div class="ci-right">${fmtDur(c.duration_s)}<br><span class="pill ${c.status}">${c.status}</span></div>
      ${c.snippet ? `<div class="ci-snip">${esc(c.snippet).replace(/&lt;(\/?)mark&gt;/g, "<$1mark>")}</div>` : ""}
    </button>`).join("");
  $$(".call-item", list).forEach((b) => b.addEventListener("click", () => openCall(Number(b.dataset.id))));
});
$("#search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loaders.history, 250); });

const listSec = (title, items, fmt = (x) => esc(x)) =>
  items?.length ? `<div class="sec"><h4>${title}</h4><ul>${items.map((x) => `<li>${fmt(x)}</li>`).join("")}</ul></div>` : "";

function transcriptHTML(segs, contact) {
  if (!segs?.length) return `<div class="empty"><p>No transcript.</p></div>`;
  return segs.map((s) => `<div class="msg ${esc(s.speaker)}"><div class="who"><span>${esc(s.speaker === "caller" ? (contact || "Caller") : s.speaker === "owner" ? "Owner" : "Assistant")}</span>${s.start_ms ? `<span>${fmtDur(s.start_ms / 1000)}</span>` : ""}${s.interrupted ? '<span class="tag warn">interrupted</span>' : ""}</div><div class="txt">${esc(s.text)}</div></div>`).join("");
}

const openCall = safe(async (id) => {
  selectedCall = id;
  $$(".call-item").forEach((b) => b.classList.toggle("sel", Number(b.dataset.id) === id));
  const c = await api(`/api/calls/${id}`);
  const a = c.analysis || {};
  const el = $("#callDetail");
  const pending = ["queued", "processing", "live"].includes(c.status);
  el.innerHTML = `
    <div class="d-head">
      <div><h2>${esc(c.title || c.contact_name || "Call #" + c.id)}</h2>
        <div class="d-meta"><span>${c.direction === "incoming" ? "Incoming" : "Outgoing"}${c.mode === "test" ? " (test)" : ""}</span>
          <span>${esc(c.contact_name || "Unknown caller")}${c.contact_phone ? " · " + esc(c.contact_phone) : ""}</span>
          <span>${fmtDate(c.started_at)}</span><span>${fmtDur(c.duration_s)}</span>
          ${c.sentiment ? `<span>Mood: ${esc(c.sentiment)}</span>` : ""}<span class="pill ${c.status}">${c.status}</span></div></div>
      <div class="d-actions">
        <button class="btn sm ghost" id="dReprocess" title="Run summary again">${ICON.refresh}Re-run</button>
        <button class="btn sm ghost" id="dDelete" title="Delete call">${ICON.trash}</button>
      </div>
    </div>
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
  const showTx = (k) => { $("#dTranscript").innerHTML = transcriptHTML(tx[k], c.contact_name); $$(".subtabs button", el).forEach((b) => b.classList.toggle("on", b.dataset.t === k)); };
  showTx("corrected");
  $$(".subtabs button", el).forEach((b) => b.addEventListener("click", () => showTx(b.dataset.t)));
  $$("input[data-aid]", el).forEach((cb) => cb.addEventListener("change", safe(async () => {
    await api(`/api/actions/${cb.dataset.aid}`, { method: "PATCH", body: { done: cb.checked } });
    cb.closest(".check").classList.toggle("done", cb.checked);
  })));
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

// ------------------------------------------------------------------ memory
loaders.memory = safe(async () => {
  const facts = await api("/api/memory");
  $("#factCount").textContent = facts.length ? `(${facts.length})` : "";
  $("#factList").innerHTML = facts.length ? facts.map((f) => `
    <div class="fact"><p>${esc(f.fact)}</p>${f.contact_name ? `<span class="who-tag">${esc(f.contact_name)}</span>` : ""}
      ${f.source_call_id ? `<span class="pill">auto</span>` : ""}
      <button class="icon-btn" data-fid="${f.id}" title="Delete">${ICON.trash}</button></div>`).join("")
    : `<div class="empty" style="padding:24px 0"><p>No facts yet. They'll appear after your first summarized call.</p></div>`;
  $$("[data-fid]").forEach((b) => b.addEventListener("click", safe(async () => { await api(`/api/memory/${b.dataset.fid}`, { method: "DELETE" }); loaders.memory(); })));
});
$("#factForm").addEventListener("submit", safe(async (e) => {
  e.preventDefault();
  await api("/api/memory", { method: "POST", body: { fact: $("#factText").value.trim(), contact_name: $("#factContact").value.trim() } });
  $("#factText").value = ""; $("#factContact").value = ""; toast("Fact added"); loaders.memory();
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
  const [{ values, voices }, dev] = await Promise.all([api("/api/settings"), api("/api/devices")]);
  $("#styleRules").value = values.style_rules;
  $("#voice").innerHTML = Object.entries(voices).map(([n, d]) => `<option value="${esc(n)}" ${n === values.voice ? "selected" : ""}>${esc(n)} — ${esc(d)}</option>`).join("");
  $("#recording").checked = !!values.recording_enabled;
  $("#silence").value = values.silence_timeout_s;
  const L = { caller_input: "Caller audio in (phone)", ai_output: "AI voice out (phone)", test_input: "Test mic", test_output: "Test headphones" };
  $("#devices").innerHTML = Object.entries(dev.resolved).map(([k, v]) =>
    `<div><small>${L[k]}</small><span class="${v.startsWith("NOT FOUND") ? "bad" : ""}">${esc(v.startsWith("NOT FOUND") ? "Not found: check .env" : v)}</span></div>`).join("");
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
    const s = await api("/api/status");
    $("#ownerLabel").textContent = `${s.owner_name}'s assistant`;
    $("#modelChip").textContent = s.live_model;
  } catch (e) { toast(e.message, true); }
  connectWS();
  const initial = location.hash.slice(1);
  showTab(TITLES[initial] ? initial : "live");
})();
