/* boardd v1 client. Vanilla JS, no build step, no CDN.
 *
 * Honesty rules the UI enforces:
 * - Liveness has three states with honest copy: Live / Delayed / Disconnected.
 *   Delayed = the stream is open but nothing (not even a heartbeat) has
 *   arrived recently. Disconnected = the stream is down.
 * - Agent-reported results always show "Boardd has not verified this."
 * - Untranslated lines are shown raw and marked "not yet translated".
 * - Every claim keeps its timestamp; ages re-render every 15 s.
 * - Anything clickable does something; nothing static looks clickable.
 */
"use strict";

const $ = (id) => document.getElementById(id);

let state = null;          // last state payload from the server
let lastMessageAt = null;  // last SSE message (state or heartbeat)
let liveness = "connecting"; // connecting | live | delayed | disconnected
let es = null;
let openLaneRef = null;
let currentMonitor = new URLSearchParams(location.search).get("monitor") || "";

// mobile shell state
let mobileTab = "lanes";       // lanes | review | activity
let mobileFilter = "all";      // all | working | blocked | done
let lastMonitors = [];         // last /api/monitors payload
let selectedMonitorIds = null; // Set, or null until monitors are known

const DELAYED_AFTER_MS = 40_000; // > 2 missed 15 s heartbeats

// ------------------------------------------------------------ theme

function applyTheme(pref) {
  if (pref === "light" || pref === "dark") document.documentElement.dataset.theme = pref;
  else delete document.documentElement.dataset.theme;
}
try { applyTheme(localStorage.getItem("boardd-theme")); } catch { /* private mode etc. */ }

$("theme-toggle").addEventListener("click", () => {
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const current = document.documentElement.dataset.theme || (dark ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem("boardd-theme", next); } catch { /* private mode etc. */ }
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/static/sw.js").catch(() => {});
}

// ------------------------------------------------------------ helpers

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "hidden") node.hidden = Boolean(v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

function ageWords(iso) {
  if (!iso) return "unknown time";
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return "unknown time";
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

function clockTime(iso) {
  if (!iso) return "--:--";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// A translated line: returns a span; raw untranslated lines get a mark.
function tline(t, { mark = true } = {}) {
  if (!t || !t.text) return el("span", {}, "");
  const span = el("span", {}, t.text);
  if (!t.translated && mark) {
    span.append(" ", el("span", { class: "untranslated" }, "(not yet translated — shown exactly as received)"));
  }
  return span;
}

// A raw-source toggle under a translated line (drawer only).
function rawBox(t) {
  if (!t || !t.raw) return null;
  const raw = el("div", { class: "raw", hidden: true }, t.raw);
  const label = el("div", { class: "rawlabel", hidden: true },
    t.translated
      ? "Canonical wording as received from the session. The sentence above is boardd's translation."
      : "This line has no translation yet; the text above is already the canonical wording.");
  const toggle = el("button", { class: "qlink toggle" }, "View raw source");
  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    const show = raw.hidden;
    raw.hidden = !show;
    label.hidden = !show;
    toggle.textContent = show ? "Hide raw source" : "View raw source";
  });
  return el("div", { class: "rawbox" }, toggle, raw, label);
}

function pill(p) {
  return el("span", { class: `pill ${p.tone}` }, p.label);
}

// ------------------------------------------------------------ liveness

function setLiveness(next) {
  liveness = next;
  renderTopbar();
}

function renderTopbar() {
  const dot = $("live-dot");
  const stateEl = $("live-state");
  const retry = $("retry");
  const ago = lastMessageAt ? ageWords(lastMessageAt) : null;
  dot.classList.remove("pulse", "warn", "bad");
  retry.hidden = true;
  if (liveness === "live") {
    dot.classList.add("pulse");
    stateEl.textContent = `Live. Last update ${ago ?? "just now"}.`;
  } else if (liveness === "delayed") {
    dot.classList.add("warn");
    stateEl.textContent = `Delayed. Last update ${ago ?? "unknown"}. Everything below is as of that update.`;
    retry.hidden = false;
  } else if (liveness === "disconnected") {
    dot.classList.add("bad");
    stateEl.textContent = lastMessageAt
      ? `Disconnected. Nothing received since ${ago}. Showing the last data boardd has.`
      : "Disconnected. No data received yet.";
    retry.hidden = false;
  } else {
    dot.classList.add("warn");
    stateEl.textContent = "Connecting…";
  }
  $("move-sentence").textContent = state ? state.summary.sentence : "";
}

function connect() {
  if (es) es.close();
  const q = currentMonitor ? `?monitor=${encodeURIComponent(currentMonitor)}` : "";
  es = new EventSource(`/events${q}`);
  setLiveness("connecting");
  es.addEventListener("state", (e) => {
    lastMessageAt = new Date().toISOString();
    state = JSON.parse(e.data);
    setLiveness("live");
    renderAll();
  });
  es.addEventListener("monitors", (e) => {
    const monitors = JSON.parse(e.data);
    renderMonitorPicker(monitors);
    cacheMonitors(monitors);
  });
  es.addEventListener("heartbeat", () => {
    lastMessageAt = new Date().toISOString();
    if (liveness !== "live") setLiveness("live");
    renderTopbar();
  });
  es.addEventListener("error", () => {
    // EventSource retries on its own; while it does, be honest.
    setLiveness(es.readyState === EventSource.CLOSED ? "disconnected" : "disconnected");
  });
}

$("retry").addEventListener("click", () => connect());

// ------------------------------------------------------------ monitor picker

function renderMonitorPicker(monitors) {
  const pick = $("monitor-pick");
  if (!monitors || monitors.length <= 1) { pick.hidden = true; return; }
  const selected = currentMonitor || monitors[0].id;
  const opts = monitors.map((m) =>
    el("option", { value: m.id },
      `${m.id} (${m.lane_count} lanes${m.needs_feedback_count ? `, ${m.needs_feedback_count} need feedback` : ""})`));
  opts.push(el("option", { value: "all" }, "all monitors"));
  pick.replaceChildren(...opts);
  pick.value = selected;
  pick.hidden = false;
}
$("monitor-pick").addEventListener("change", (e) => {
  currentMonitor = e.target.value;
  const url = new URL(location.href);
  url.searchParams.set("monitor", currentMonitor);
  history.replaceState(null, "", url);
  connect();
});

fetch("/api/monitors").then((r) => r.json()).then((monitors) => {
  renderMonitorPicker(monitors);
  cacheMonitors(monitors);
}).catch(() => {});

setInterval(() => {
  if (liveness === "live" && lastMessageAt &&
      Date.now() - new Date(lastMessageAt).getTime() > DELAYED_AFTER_MS) {
    setLiveness("delayed");
  }
  renderTopbar();
  renderAges();
}, 5000);

// ------------------------------------------------------------ rendering

function renderAll() {
  if (!state) return;
  renderTopbar();
  renderBanners();
  renderSince();
  renderNeeds();
  $("summary-sentence").textContent = state.summary.sentence;
  renderLanes();
  renderHistory();
  renderOpsFooter();
  renderMobile();
  if (openLaneRef) {
    const lane = state.lanes.find((l) => l.session_ref === openLaneRef);
    if (lane) renderDrawer(lane); else closeDrawer();
  }
}

function renderAges() {
  // Ages are re-rendered wholesale; cheap at this scale.
  if (state && liveness !== "connecting") {
    renderSince();
    renderLanes();
    renderMobile();
  }
}

function renderBanners() {
  const box = $("banners");
  box.replaceChildren();
  for (const err of state.source.errors) {
    box.append(el("div", { class: "banner bad" },
      `A state file could not be read: ${err}. What is shown below may be incomplete.`));
  }
  if (state.source.data_stale) {
    box.append(el("div", { class: "banner" },
      `The fleet state file was last written ${ageWords(state.source.goals_updated_at)}. `
      + "The connection may be fine; the daemons have simply not written anything newer."));
  }
  if (state.source.note) {
    box.append(el("div", { class: "banner" }, state.source.note));
  }
}

function renderSince() {
  const list = $("since-list");
  list.replaceChildren();
  const events = state.events.slice(0, 5);
  if (!events.length) {
    list.append(el("div", { class: "item" }, "Nothing has changed since the last sweep."));
    return;
  }
  for (const ev of events) {
    const item = el("div", { class: "item" });
    const lane = state.lanes.find((l) => l.session_ref === ev.lane);
    item.append(
      el("div", {},
        lane ? el("b", {}, lane.title + " — ") : null,
        tline(ev.summary),
        " ",
        el("span", { class: "evid reported" }, ev.verified_label)),
      el("div", { class: "when" }, `${ageWords(ev.ts)} · ${clockTime(ev.ts)}`),
    );
    list.append(item);
  }
}

async function postLaneAction(laneId, action, body) {
  const q = currentMonitor && currentMonitor !== "all" ? `?monitor=${encodeURIComponent(currentMonitor)}` : "";
  const r = await fetch(`/api/lanes/${encodeURIComponent(laneId)}/${action}${q}`, {
    method: "POST",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `${action} failed`);
  return data;
}

function renderNeeds() {
  const zone = $("zone-needs");
  const card = $("needs-card");
  card.replaceChildren();
  card.classList.remove("quiet");
  // Oldest-ask-first: the server already sorts state.needs_you this way.
  const items = state.needs_you;
  if (!items.length) {
    // Healthy state: one quiet line, no standing red zone.
    zone.hidden = false;
    card.classList.add("quiet");
    card.append(el("span", { class: "ok" }, "✓"),
      el("span", { class: "msg" }, "Nothing needs feedback."));
    return;
  }
  zone.hidden = false;
  card.append(el("div", { class: "ny-head" },
    el("h3", {}, "Needs feedback"),
    el("span", { class: "ny-count" },
      `${items.length === 1 ? "one lane" : items.length + " lanes"} waiting, oldest first`)));
  for (const item of items) {
    const copyBtn = el("button", { class: "btn quiet" }, "Copy question for Ramble");
    copyBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await navigator.clipboard.writeText(`${item.lane_title}: ${item.question.text}`);
        copyBtn.textContent = "Copied";
        setTimeout(() => { copyBtn.textContent = "Copy question for Ramble"; }, 1500);
      } catch {
        copyBtn.textContent = "Copy failed — select the text instead";
      }
    });
    const openBtn = el("button", { class: "btn quiet" }, "Open lane");
    openBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const lane = state.lanes.find((l) => l.session_ref === item.lane_ref);
      if (lane) openDrawer(lane);
    });
    const ackBtn = el("button", { class: "btn quiet" }, "Ack");
    ackBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      ackBtn.textContent = "Acking…";
      try { await postLaneAction(item.lane_id, "ack"); connect(); }
      catch (err) { ackBtn.textContent = String(err.message || err); return; }
      ackBtn.textContent = "Acked";
    });
    const answerBox = el("textarea", { placeholder: "Answer, then Send", rows: "2" });
    const answerBtn = el("button", { class: "btn quiet" }, "Send answer");
    answerBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const text = answerBox.value.trim();
      if (!text) return;
      answerBtn.textContent = "Sending…";
      try { await postLaneAction(item.lane_id, "answer", { text }); connect(); }
      catch (err) { answerBtn.textContent = String(err.message || err); return; }
      answerBtn.textContent = "Sent";
    });
    card.append(el("div", { class: "ruling" },
      el("div", { class: "what" }, el("b", {}, item.lane_title), " asks: ", tline(item.question)),
      el("div", { class: "why" }, el("b", {}, "Goal: "), tline(item.goal)),
      el("div", { class: "why" }, item.context),
      el("div", { class: "acts" }, copyBtn, openBtn, ackBtn),
      answerBox,
      el("div", { class: "acts" }, answerBtn)));
  }
}

function renderLanes() {
  const grid = $("lanegrid");
  grid.replaceChildren();
  for (const lane of state.lanes) {
    grid.append(laneCard(lane));
  }
}

// Card order (operator's ruling): title, GOAL, movement, latest result, done summary.
function laneCard(lane) {
  const card = el("article", {
    class: "lane", role: "button", tabindex: "0",
    "aria-label": `Open details for ${lane.title}`,
  });
  card.addEventListener("click", () => openDrawer(lane));
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDrawer(lane); }
  });

  card.append(el("div", { class: "head" },
    el("span", { class: "title" }, lane.title),
    lane.needs_review ? el("span", { class: "badge review" }, "Needs feedback") : null,
    el("span", { class: "age" }, `Updated ${ageWords(lane.updated_ts)}`)));

  card.append(el("div", { class: "goalline" }, el("b", {}, "Goal: "), tline(lane.goal)));

  if (lane.scope.narrowed) {
    const dropped = lane.scope.dropped.map((d) => d.text).join(" ");
    card.append(el("div", { class: "attn scope" },
      el("b", {}, "Scope is narrower than enrollment. "), `No longer required: ${dropped}`));
  }

  card.append(el("div", { class: "move" }, pill(lane.movement.pill), lane.movement.sentence));

  const result = el("div", { class: "result" }, el("b", {}, "Latest result: "));
  if (lane.latest_result) {
    result.append(tline(lane.latest_result.summary), " ",
      el("span", { class: "evid reported" }, lane.latest_result.verified_label));
  } else {
    result.append("no finished result reported yet.");
  }
  card.append(result);

  card.append(el("div", { class: "donewhen" },
    el("b", {}, "Done when: "), lane.done_when.summary,
    ` (${lane.done_when.total} condition${lane.done_when.total === 1 ? "" : "s"} — click for the list)`));

  card.append(el("div", { class: "ops" },
    el("code", {}, lane.session_ref),
    el("span", {}, lane.goal_version != null ? `goal v${lane.goal_version}` : "")));
  return card;
}

// ------------------------------------------------------------ drawer

function openDrawer(lane) {
  openLaneRef = lane.session_ref;
  renderDrawer(lane);
  $("scrim").hidden = false;
  $("drawer").hidden = false;
}

function closeDrawer() {
  openLaneRef = null;
  $("scrim").hidden = true;
  $("drawer").hidden = true;
}

$("scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

const DMARKS = { verified: ["v", "✓"], pending: ["n", "○"], unbound: ["n", "–"], review: ["q", "?"] };

function renderDrawer(lane) {
  const drawer = $("drawer");
  drawer.replaceChildren();

  const closeBtn = el("button", { class: "close" }, "Close ✕");
  closeBtn.addEventListener("click", closeDrawer);
  drawer.append(el("div", { class: "d-head" },
    el("div", { class: "row" },
      el("h4", {}, lane.title),
      el("span", { class: "age" }, `Updated ${ageWords(lane.updated_ts)}`),
      closeBtn)));

  const body = el("div", { class: "d-body" });

  const goalSec = el("div", { class: "d-sec" }, el("h5", {}, "Goal"),
    el("p", {}, tline(lane.goal)));
  const goalRaw = rawBox(lane.goal);
  if (goalRaw) goalSec.append(goalRaw);
  body.append(goalSec);

  if (lane.intent) {
    body.append(el("div", { class: "d-sec" }, el("h5", {}, "Why this lane exists"),
      el("p", {}, tline(lane.intent))));
  }

  if (lane.scope.narrowed) {
    const dropped = lane.scope.dropped.map((d) => d.text).join(" ");
    body.append(el("div", { class: "d-sec" }, el("h5", {}, "Scope change"),
      el("p", { class: "attn scope", style: "display:block" },
        el("b", {}, "Scope is narrower than enrollment. "),
        `No longer required: ${dropped} `,
        "The monitor recorded this narrowing. It stays visible until the lane closes.")));
  }

  const dod = el("ul", { class: "dod" });
  for (const cond of lane.done_when.conditions) {
    const [cls, glyph] = DMARKS[cond.proof.state] ?? DMARKS.unbound;
    dod.append(el("li", {},
      el("span", { class: `dmark ${cls}` }, glyph),
      el("span", { class: "cond" }, tline(cond),
        el("span", { class: `lbl ${cls}` }, cond.proof.label))));
  }
  body.append(el("div", { class: "d-sec" }, el("h5", {}, "Done when"), dod,
    el("p", { class: "soft", style: "margin-top:0.45rem" }, lane.done_when.summary)));

  const nowSec = el("div", { class: "d-sec" }, el("h5", {}, "What the agent is doing now"),
    el("p", {}, tline(lane.movement.now)));
  const nowRaw = rawBox(lane.movement.now);
  if (nowRaw) nowSec.append(nowRaw);
  if (lane.movement.hold_reason) {
    nowSec.append(el("p", { class: "soft", style: "margin-top:0.35rem" },
      "Held: ", tline(lane.movement.hold_reason)));
  }
  body.append(nowSec);

  if (lane.open_asks.length) {
    const asks = el("ul", { class: "dod" });
    for (const ask of lane.open_asks) {
      asks.append(el("li", {}, el("span", { class: "dmark q" }, "?"),
        el("span", { class: "cond" }, tline(ask))));
    }
    body.append(el("div", { class: "d-sec" }, el("h5", {}, "Waiting on you"), asks));
  }

  const laneEvents = state.events.filter((ev) => ev.lane === lane.session_ref);
  if (laneEvents.length) {
    const tl = el("ul", { class: "timeline" });
    for (const ev of laneEvents) {
      const entry = el("span", { class: "e" }, tline(ev.summary), " ",
        el("span", { class: "evid reported" }, ev.verified_label));
      const evRaw = rawBox(ev.summary);
      if (evRaw) entry.append(evRaw);
      tl.append(el("li", {}, el("span", { class: "t" }, clockTime(ev.ts)), entry));
    }
    body.append(el("div", { class: "d-sec" }, el("h5", {}, "Meaningful changes"), tl));
  }

  drawer.append(body);
  drawer.append(el("div", { class: "d-ops" },
    el("h5", {}, "Operations"),
    el("div", { class: "oprow" },
      el("span", {}, "Session ", el("code", {}, lane.session_ref)),
      el("span", {}, "Goal file ", el("code", {}, "goals.json")),
      el("span", {}, lane.goal_version != null ? `goal v${lane.goal_version}` : ""))));
}

// ------------------------------------------------------------ history

const HIST_LEGEND = [
  ["ok", "Progress", "the monitor reported forward movement"],
  ["warn", "Reported done", "an agent claims completion; boardd has not verified it"],
  ["bad", "Blocked", "the lane cannot move without an answer"],
  ["hold", "Holding / no change", "paused on purpose, or nothing happened"],
  ["ramble", "From your Ramble", "something you proposed in conversation"],
];

function renderHistory() {
  const legend = $("hist-legend");
  legend.replaceChildren("Tags are boardd's plain-words reading of each monitor summary: ");
  for (const [tone, label, meaning] of HIST_LEGEND) {
    legend.append(pill({ tone, label }), ` ${meaning}. `);
  }

  const groups = $("hist-groups");
  groups.replaceChildren();
  const byDay = new Map();
  for (const ev of state.events) {
    const day = ev.ts ? new Date(ev.ts).toDateString() : "Undated";
    if (!byDay.has(day)) byDay.set(day, []);
    byDay.get(day).push(ev);
  }
  for (const [day, events] of byDay) {
    const cardEl = el("div", { class: "card hist-card" });
    for (const ev of events) {
      const lane = state.lanes.find((l) => l.session_ref === ev.lane);
      cardEl.append(el("div", { class: "hist-row" },
        el("span", { class: "t" }, clockTime(ev.ts)),
        el("span", { class: "cat" }, pill({ tone: ev.category.tone, label: ev.category.label })),
        el("div", { class: "what" },
          lane ? el("b", {}, lane.title + " — ") : null,
          tline(ev.summary),
          " ",
          el("span", { class: "evid reported" }, ev.verified_label))));
    }
    groups.append(el("div", { class: "hist-group" }, el("h4", {}, day), cardEl));
  }
  if (!byDay.size) {
    groups.append(el("p", { class: "movesum" }, "No history yet: the sweep digest is empty."));
  }
}

// ------------------------------------------------------------ ops footer

function renderOpsFooter() {
  const foot = $("opsfoot");
  foot.replaceChildren(
    el("span", {}, "Operations"),
    el("span", {}, "State dir ", el("code", {}, state.source.state_dir)),
    el("span", {}, "Sweep at ", el("code", {}, state.source.sweep_at ?? "unknown")),
    el("a", { class: "qlink", href: "/api/state" }, "Raw state JSON"),
  );
}

// ------------------------------------------------------------ tabs

function showTab(name) {
  for (const [tab, view] of [["cockpit", "view-cockpit"], ["history", "view-history"], ["trail", "view-trail"]]) {
    const on = tab === name;
    $(view).hidden = !on;
    $(`tab-${tab}`).classList.toggle("on", on);
    $(`tab-${tab}`).setAttribute("aria-selected", String(on));
  }
  if (name === "trail") {
    const frame = $("trail-frame");
    const url = state && state.agenttrail_url;
    if (url && frame.src !== url) frame.src = url;
  }
}
$("tab-cockpit").addEventListener("click", () => showTab("cockpit"));
$("tab-history").addEventListener("click", () => showTab("history"));
$("tab-trail").addEventListener("click", () => showTab("trail"));

// ------------------------------------------------------------ mobile shell
//
// Single column under 600px: Lanes / Review / Activity behind a bottom tab
// bar (Main.dc.html / Review.dc.html / Monitors.dc.html). Reuses the same
// `state` and SSE connection as the wide layout above; only the rendering
// and the monitor-picker UI differ.

const STATUS_META = {
  working: { label: "Working", tone: "success", group: "working" },
  held: { label: "Held", tone: "neutral", group: "working" },
  idle: { label: "Idle", tone: "neutral", group: "working" },
  "turn-finished-unverified": { label: "Unverified", tone: "purple", group: "working" },
  "completion-disputed": { label: "Disputed", tone: "danger", group: "working" },
  "done-pending-verification": { label: "Verifying", tone: "success", group: "working" },
  blocked: { label: "Blocked", tone: "accent", group: "blocked" },
  "done-pending-close": { label: "Done", tone: "success", group: "done" },
};
function statusMeta(status) {
  return STATUS_META[status] || { label: status || "Unknown", tone: "neutral", group: "working" };
}

const DEFAULT_NUDGE_TEXT = "Nudge: please post a status update on this lane.";

function laneNowLine(lane) {
  if (lane.open_asks.length) return el("span", {}, "Asks: ", tline(lane.open_asks[0]));
  if (lane.movement.status === "working" && lane.movement.now && lane.movement.now.text) {
    return el("span", {}, "Now: ", tline(lane.movement.now));
  }
  return el("span", {}, lane.movement.sentence); // already a plain server-authored sentence
}

function showToast(message) {
  const toast = $("m-toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => { toast.hidden = true; }, 4000);
}

function renderMobile() {
  if (!state) return;
  renderMobileHeader();
  renderMobileChips();
  renderMobileBanner();
  renderMobileLanes();
  renderMobileReview();
  renderMobileBadge();
}

function renderMobileHeader() {
  if (mobileTab === "review") {
    $("m-title").textContent = "Needs you";
    const n = state.needs_you.length;
    $("m-subtitle").textContent = n ? `${n} open · oldest first` : "Nothing open";
  } else {
    $("m-title").textContent = "Lanes";
    const when = state.source.goals_updated_at ? clockTime(state.source.goals_updated_at) : "--:--";
    const monitorLabel = state.monitor && state.monitor !== "all" ? state.monitor : "all monitors";
    $("m-subtitle").textContent = `Updated ${when} · ${monitorLabel}`;
  }
  $("m-monitor-label").textContent = currentMonitor === "all" || !currentMonitor ? "All" : currentMonitor;
  const anyActive = lastMonitors.some((m) => m.unit_active_state === "active");
  $("m-monitor-dot").style.background = anyActive || !lastMonitors.length ? "var(--mb-success)" : "var(--mb-dim)";
}

const CHIP_DEFS = [["all", "All"], ["working", "Working"], ["blocked", "Blocked"], ["done", "Done"]];

function renderMobileChips() {
  const box = $("m-chips");
  box.replaceChildren();
  const counts = { all: state.lanes.length, working: 0, blocked: 0, done: 0 };
  for (const lane of state.lanes) counts[statusMeta(lane.movement.status).group]++;
  for (const [key, label] of CHIP_DEFS) {
    const chip = el("button", { class: `m-chip${mobileFilter === key ? " on" : ""}` }, `${label} · ${counts[key]}`);
    chip.addEventListener("click", () => { mobileFilter = key; renderMobile(); });
    box.append(chip);
  }
}

function renderMobileBanner() {
  const banner = $("m-banner");
  const items = state.needs_you;
  if (!items.length) { banner.hidden = true; return; }
  banner.hidden = false;
  banner.replaceChildren(
    el("div", {},
      el("div", { class: "m-banner-title" }, `${items.length} lane${items.length === 1 ? "" : "s"} need you`),
      el("div", { class: "m-banner-sub" }, `Oldest ask waiting ${ageWords(items[0].since).replace(/ ago$/, "")}`)),
    el("span", { class: "m-banner-cta" }, "Review"));
}
$("m-banner").addEventListener("click", () => showMobileTab("review"));

function renderMobileLanes() {
  const list = $("m-lanelist");
  list.replaceChildren();
  const lanes = state.lanes.filter(
    (lane) => mobileFilter === "all" || statusMeta(lane.movement.status).group === mobileFilter);
  if (!lanes.length) {
    list.append(el("div", { class: "m-review-empty" }, "No lanes match this filter."));
    return;
  }
  for (const lane of lanes) list.append(mobileLaneCard(lane));
}

function mobileLaneCard(lane) {
  const meta = statusMeta(lane.movement.status);
  const classes = ["m-lane"];
  if (lane.movement.status === "working") classes.push("m-lane-tint");
  if (meta.group === "done") classes.push("m-lane-done");
  const card = el("article", { class: classes.join(" "), "data-lane-ref": lane.session_ref });
  card.append(
    el("div", { class: "m-lane-head" },
      el("span", { class: "m-lane-id" }, lane.lane_id),
      el("span", { class: `m-badge-pill ${meta.tone}` }, meta.label)),
    el("div", { class: "m-lane-goal" }, tline(lane.goal)),
    el("div", { class: "m-lane-now" }, laneNowLine(lane)));
  return card;
}

function renderMobileBadge() {
  const badge = $("m-review-badge");
  const n = state.needs_you.length;
  badge.hidden = !n;
  badge.textContent = String(n);
}

function reviewActionKind(item) {
  const lane = state.lanes.find((l) => l.session_ref === item.lane_ref);
  if (lane && lane.open_asks.length) return "ask";
  const status = lane ? lane.movement.status : null;
  if (status === "completion-disputed" || status === "done-pending-verification") return "disputed";
  return "nudge"; // turn-finished-unverified, blocked with no literal ask, or unknown
}

function renderMobileReview() {
  const list = $("m-reviewlist");
  list.replaceChildren();
  if (!state.needs_you.length) {
    list.append(el("div", { class: "m-review-empty" }, "Nothing needs feedback."));
    return;
  }
  for (const item of state.needs_you) list.append(mobileReviewCard(item));
}

function mobileReviewCard(item) {
  const lane = state.lanes.find((l) => l.session_ref === item.lane_ref);
  const kind = reviewActionKind(item);
  const meta = kind === "ask" ? { label: "Asks", tone: "accent" } : statusMeta(lane ? lane.movement.status : "");
  const card = el("div", { class: "m-review-card" });
  card.append(
    el("div", { class: "m-review-head" },
      el("span", { class: "m-lane-id" }, `${item.lane_id} · ${ageWords(item.since).replace(/ ago$/, "")}`),
      el("span", { class: `m-badge-pill ${meta.tone}` }, meta.label)),
    el("div", { class: "m-review-ask" }, tline(item.question)),
    el("div", { class: "m-review-goal" }, "Goal: ", tline(item.goal)));

  const removeCard = () => {
    state.needs_you = state.needs_you.filter((i) => i !== item);
    renderMobile();
  };
  const fail = (btn, label, err) => {
    btn.disabled = false;
    btn.textContent = label;
    showToast(String(err.message || err));
  };

  // A no-op resolve (no open ask on the lane) now comes back as an error
  // response, not a false 200 — postLaneAction throws and `fail` runs
  // instead of removeCard. `result.changed` is checked too: the API
  // contract promises it, even though today every 200 already implies it.
  if (kind === "nudge") {
    const nudgeBtn = el("button", { class: "m-review-btn quiet" }, "Nudge");
    nudgeBtn.addEventListener("click", async () => {
      nudgeBtn.disabled = true; nudgeBtn.textContent = "Sending…";
      try {
        const result = await postLaneAction(item.lane_id, "answer", { text: DEFAULT_NUDGE_TEXT });
        if (result.changed) removeCard();
        else fail(nudgeBtn, "Nudge", new Error("Nothing to nudge — no open ask on this lane."));
      } catch (err) { fail(nudgeBtn, "Nudge", err); }
    });
    const openBtn = el("button", { class: "m-review-btn primary" }, "Open lane");
    openBtn.addEventListener("click", () => openLaneInLanes(item.lane_ref));
    card.append(el("div", { class: "m-review-acts" }, nudgeBtn, openBtn));
    return card;
  }

  // "disputed" (completion-disputed / done-pending-verification) only
  // changes what the badge shows above; chitra-goals has no verb that
  // closes or disputes a done-pending lane (only resolve-ask, which needs
  // a literal open ask), so it gets the same ack/answer pair as "ask" —
  // never the "Accept done"/"Send back" labels this UI used to show for
  // an action the backend never actually took.
  const input = el("input", { class: "m-review-input", type: "text", placeholder: "Type an answer" });
  const primaryLabel = "Send answer";
  const secondaryLabel = "Acknowledge";
  const secondaryBtn = el("button", { class: "m-review-btn quiet" }, secondaryLabel);
  secondaryBtn.addEventListener("click", async () => {
    secondaryBtn.disabled = true; secondaryBtn.textContent = "…";
    try {
      const result = await postLaneAction(item.lane_id, "ack");
      if (result.changed) removeCard();
      else fail(secondaryBtn, secondaryLabel, new Error("Nothing to acknowledge — no open ask on this lane."));
    } catch (err) { fail(secondaryBtn, secondaryLabel, err); }
  });
  const primaryBtn = el("button", { class: "m-review-btn primary" }, primaryLabel);
  primaryBtn.addEventListener("click", async () => {
    const text = input.value.trim();
    if (!text) { input.focus(); return; }
    primaryBtn.disabled = true; primaryBtn.textContent = "…";
    try {
      const result = await postLaneAction(item.lane_id, "answer", { text });
      if (result.changed) removeCard();
      else fail(primaryBtn, primaryLabel, new Error("Nothing to answer — no open ask on this lane."));
    } catch (err) { fail(primaryBtn, primaryLabel, err); }
  });
  card.append(input, el("div", { class: "m-review-acts" }, secondaryBtn, primaryBtn));
  return card;
}

function openLaneInLanes(laneRef) {
  mobileFilter = "all";
  showMobileTab("lanes");
  renderMobile();
  requestAnimationFrame(() => {
    const target = document.querySelector(`.m-lane[data-lane-ref="${CSS.escape(laneRef)}"]`);
    if (!target) return;
    target.scrollIntoView({ block: "center", behavior: "smooth" });
    target.style.outline = "2px solid var(--mb-accent)";
    setTimeout(() => { target.style.outline = ""; }, 1600);
  });
}

function showMobileTab(name) {
  mobileTab = name;
  for (const tab of ["lanes", "review", "activity"]) {
    const on = tab === name;
    $(`m-view-${tab}`).hidden = !on;
    $(`m-tab-${tab}`).classList.toggle("on", on);
    $(`m-tab-${tab}`).setAttribute("aria-selected", String(on));
  }
  if (name === "activity") {
    const frame = $("m-trail-frame");
    const url = state && state.agenttrail_url;
    if (url && frame.src !== url) frame.src = url;
  }
  if (state) renderMobileHeader();
}
$("m-tab-lanes").addEventListener("click", () => showMobileTab("lanes"));
$("m-tab-review").addEventListener("click", () => showMobileTab("review"));
$("m-tab-activity").addEventListener("click", () => showMobileTab("activity"));

// ---- monitor picker sheet (Monitors.dc.html) ----

function cacheMonitors(monitors) {
  lastMonitors = monitors || [];
  const availableIds = lastMonitors.filter((m) => m.has_state_root).map((m) => m.id);
  if (selectedMonitorIds === null) {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem("boardd-monitors-selected") || "null"); } catch { /* private mode */ }
    selectedMonitorIds = Array.isArray(saved) && saved.length && saved.every((id) => availableIds.includes(id))
      ? new Set(saved) : new Set(availableIds);
  } else {
    // Drop selections for monitors that vanished between discovery ticks.
    for (const id of [...selectedMonitorIds]) if (!availableIds.includes(id)) selectedMonitorIds.delete(id);
  }
  renderMonitorSheet();
  renderMobile();
}

function renderMonitorSheet() {
  const rows = $("m-sheet-rows");
  rows.replaceChildren();
  for (const m of lastMonitors) {
    const selectable = m.has_state_root;
    const selected = selectable && selectedMonitorIds.has(m.id);
    const dotColor = !selectable ? "var(--mb-dim)" : m.unit_active_state === "active" ? "var(--mb-success)" : "var(--mb-accent)";
    const sub = selectable
      ? `${m.lane_count} lane${m.lane_count === 1 ? "" : "s"}${m.needs_feedback_count ? ` · ${m.needs_feedback_count} need you` : ""}`
      : "No state root yet";
    const row = el("div", { class: `m-sheet-row${selected ? " selected" : ""}${selectable ? "" : " disabled"}` },
      el("span", { class: "m-sheet-row-dot", style: `background:${dotColor}` }),
      el("div", { class: "m-sheet-row-body" },
        el("div", { class: "m-sheet-row-name" }, m.id),
        el("div", { class: "m-sheet-row-sub" }, sub)),
      el("span", { class: `m-sheet-row-check${selected ? " checked" : ""}` }, selected ? "✓" : ""));
    if (selectable) {
      row.addEventListener("click", () => {
        if (selectedMonitorIds.has(m.id)) selectedMonitorIds.delete(m.id); else selectedMonitorIds.add(m.id);
        renderMonitorSheet();
      });
    }
    rows.append(row);
  }
  const availableIds = lastMonitors.filter((m) => m.has_state_root).map((m) => m.id);
  const allSelected = availableIds.length > 0 && availableIds.every((id) => selectedMonitorIds.has(id));
  $("m-sheet-apply").textContent = allSelected || selectedMonitorIds.size === 0
    ? "Show all selected" : `Show ${selectedMonitorIds.size} selected`;
}

function openMonitorSheet() {
  renderMonitorSheet();
  $("m-sheet-scrim").hidden = false;
  $("m-sheet").hidden = false;
}
function closeMonitorSheet() {
  $("m-sheet-scrim").hidden = true;
  $("m-sheet").hidden = true;
}
$("m-monitor-btn").addEventListener("click", openMonitorSheet);
$("m-sheet-scrim").addEventListener("click", closeMonitorSheet);
$("m-sheet-apply").addEventListener("click", () => {
  try { localStorage.setItem("boardd-monitors-selected", JSON.stringify([...selectedMonitorIds])); } catch { /* private mode */ }
  const availableIds = lastMonitors.filter((m) => m.has_state_root).map((m) => m.id);
  const allSelected = availableIds.length > 0 && availableIds.every((id) => selectedMonitorIds.has(id));
  let next;
  if (allSelected || selectedMonitorIds.size === 0) next = "all";
  else if (selectedMonitorIds.size === 1) next = [...selectedMonitorIds][0];
  // ponytail: the server only understands one monitor id or "all"; a
  // genuine partial subset (2 of 3+) has no union endpoint yet, so it
  // degrades to "all" until boardd grows a comma-separated filter.
  else next = "all";
  currentMonitor = next;
  const url = new URL(location.href);
  url.searchParams.set("monitor", currentMonitor);
  history.replaceState(null, "", url);
  closeMonitorSheet();
  connect();
});

// ------------------------------------------------------------ boot

{
  const q = currentMonitor ? `?monitor=${encodeURIComponent(currentMonitor)}` : "";
  fetch(`/api/state${q}`).then((r) => r.json()).then((s) => {
    if (!state) { state = s; renderAll(); }
  }).catch(() => { /* SSE will state the truth in the top bar */ });
}
connect();
