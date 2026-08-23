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

const DELAYED_AFTER_MS = 40_000; // > 2 missed 15 s heartbeats

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

function joinedLine(value, fallback = "unknown") {
  return value && value.text ? tline(value) : el("span", {}, fallback);
}

function joinedProgress(progress) {
  const known = progress && progress.percentage != null;
  const percentage = known ? Math.max(0, Math.min(100, Number(progress.percentage))) : 0;
  const label = known
    ? String(progress.percentage) + "% (" + progress.completed_steps + "/" + progress.total_steps + ")"
    : "unavailable (" + (progress?.reason ?? "no lane update has been observed") + ")";
  return el("div", { class: "joined-progress" },
    el("div", { class: "joined-progress-head" },
      el("b", {}, "Progress"),
      el("span", {}, label)),
    el("div", { class: "progress-track", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100",
      "aria-valuenow": known ? String(progress.percentage) : "0", "aria-label": label },
      el("div", { class: "progress-fill" + (known ? "" : " unknown"), style: "width:" + percentage + "%" })),
  );
}

function joinedProblemSummary(report) {
  const open = report.open_problems?.length ?? 0;
  const resolved = report.resolved_problems?.length ?? 0;
  const summary = el("div", { class: "joined-problems" + (open ? " open" : "") },
    el("b", {}, "Problems: "),
    String(open) + " open · " + String(resolved) + " resolved");
  if (open) {
    summary.append(el("div", { class: "joined-problem-line" },
      el("b", {}, "Open: "),
      ...report.open_problems.map((problem) => joinedLine(problem.summary, problem.id)).reduce((children, value, index) => {
        if (index) children.push("; ");
        children.push(value);
        return children;
      }, [])));
  }
  if (resolved) {
    summary.append(el("div", { class: "joined-problem-line" },
      el("b", {}, "Resolved: "),
      ...report.resolved_problems.map((problem) => joinedLine(problem.summary, problem.id)).reduce((children, value, index) => {
        if (index) children.push("; ");
        children.push(value);
        return children;
      }, [])));
  }
  return summary;
}

function joinedSummary(report) {
  const position = report.roadmap?.position;
  const owner = report.owner?.id ?? "unknown";
  const provider = report.provider ? report.provider.kind + " · " + report.provider.handle : "unknown";
  const summary = el("div", { class: "joined-summary" });
  const check = report.next_check;
  summary.append(
    joinedProgress(report.progress),
    el("div", { class: "joined-line" },
      el("b", {}, "Road map: "),
      position ? el("span", {}, joinedLine(position.title, position.id), " (", position.status, ")") : "unknown position"),
    el("div", { class: "joined-line" },
      el("b", {}, "NOW: "),
      joinedLine(report.now)),
    el("div", { class: "joined-line" },
      el("b", {}, "NEXT: "),
      joinedLine(report.next)),
    el("div", { class: "joined-line" },
      el("b", {}, "CHECK: "),
      check ? String(check.at) + " — " : "unknown — ",
      check ? joinedLine(check.reason) : "no durable check recorded"),
    el("div", { class: "joined-meta" },
      "Owner: " + owner + " · Provider: " + provider),
    joinedProblemSummary(report),
  );
  if (report.recovery_action) {
    summary.append(el("div", { class: "joined-line" },
      el("b", {}, "Recovery: "),
      joinedLine(report.recovery_action)));
  }
  return summary;
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
  es = new EventSource("/events");
  setLiveness("connecting");
  es.addEventListener("state", (e) => {
    lastMessageAt = new Date().toISOString();
    state = JSON.parse(e.data);
    setLiveness("live");
    renderAll();
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

function renderNeeds() {
  const zone = $("zone-needs");
  const card = $("needs-card");
  card.replaceChildren();
  card.classList.remove("quiet");
  const items = state.needs_you;
  if (!items.length) {
    // Healthy state: one quiet line, no standing red zone.
    zone.hidden = false;
    card.classList.add("quiet");
    card.append(el("span", { class: "ok" }, "✓"),
      el("span", { class: "msg" }, "Nothing is waiting on you."));
    return;
  }
  zone.hidden = false;
  card.append(el("div", { class: "ny-head" },
    el("h3", {}, "Needs you"),
    el("span", { class: "ny-count" },
      `${items.length === 1 ? "one decision" : items.length + " decisions"} waiting`)));
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
    card.append(el("div", { class: "ruling" },
      el("div", { class: "what" }, el("b", {}, item.lane_title), " asks: ", tline(item.question)),
      el("div", { class: "why" }, item.context),
      el("div", { class: "acts" }, copyBtn, openBtn)));
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
    el("span", { class: "age" }, `Updated ${ageWords(lane.updated_ts)}`)));

  const joined = lane.joined_session;
  card.append(el("div", { class: "goalline" }, el("b", {}, "Goal: "), tline(joined?.goal ?? lane.goal)));

  if (joined) {
    card.append(joinedSummary(joined));
    card.append(el("div", { class: "ops" },
      el("code", {}, lane.session_ref),
      el("span", {}, lane.goal_version != null ? "goal v" + lane.goal_version : "")));
    return card;
  }

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

function joinedProblemSection(label, problems) {
  const list = el("ul", { class: "dod" });
  if (!problems?.length) {
    list.append(el("li", {}, "None recorded."));
  } else {
    for (const problem of problems) {
      const detail = el("li", {},
        el("span", { class: "dmark " + (problem.state === "open" ? "q" : "v") },
          problem.state === "open" ? "!" : "✓"),
        el("span", { class: "cond" },
          el("b", {}, problem.id + ": "),
          joinedLine(problem.summary),
          problem.need ? el("span", { class: "lbl q" }, "Need: ", joinedLine(problem.need)) : null,
          problem.resolution ? el("span", { class: "lbl v" }, "Resolved: ", joinedLine(problem.resolution)) : null));
      list.append(detail);
    }
  }
  return el("div", { class: "d-sec" }, el("h5", {}, label), list);
}

function joinedDetails(report) {
  const root = el("div", { class: "joined-details" });
  const goalSec = el("div", { class: "d-sec" }, el("h5", {}, "Goal"),
    el("p", {}, joinedLine(report.goal)));
  const progressSec = el("div", { class: "d-sec" }, el("h5", {}, "Progress"), joinedProgress(report.progress));
  const position = report.roadmap?.position;
  const roadmap = el("div", { class: "d-sec" },
    el("h5", {}, "Road map"),
    el("p", {}, "Version ", report.roadmap?.version ?? "unknown", " · ", report.roadmap?.assessment ?? "unknown"),
    el("p", { class: "soft" }, "Position: ",
      position ? el("span", {}, joinedLine(position.title, position.id), " (", position.status, ")") : "unknown"),
  );
  const steps = el("ul", { class: "dod" });
  for (const step of report.roadmap?.steps ?? []) {
    steps.append(el("li", {},
      el("span", { class: "dmark " + (step.status === "done" ? "v" : step.status === "blocked" ? "q" : "n") },
        step.status === "done" ? "✓" : step.status === "blocked" ? "!" : "○"),
      el("span", { class: "cond" }, joinedLine(step.title, step.id),
        el("span", { class: "lbl " + (step.status === "active" ? "v" : "n") }, step.status))));
  }
  if (report.roadmap?.steps?.length) roadmap.append(steps);
  const now = el("div", { class: "d-sec" }, el("h5", {}, "NOW"), el("p", {}, joinedLine(report.now)));
  const next = el("div", { class: "d-sec" }, el("h5", {}, "NEXT"), el("p", {}, joinedLine(report.next)));
  const check = report.next_check
    ? el("p", {}, report.next_check.at, " — ", joinedLine(report.next_check.reason),
      report.next_check.wake_condition ? el("span", { class: "soft" }, "Wake: ", joinedLine(report.next_check.wake_condition)) : null)
    : el("p", {}, "Unknown — no durable check recorded.");
  const checkSec = el("div", { class: "d-sec" }, el("h5", {}, "CHECK"), check);
  const ownerSec = el("div", { class: "d-sec" }, el("h5", {}, "Owner and provider"),
    el("p", {}, (report.owner?.id ?? "unknown") + " · " +
      (report.provider ? report.provider.kind + " · " + report.provider.handle : "unknown")));
  root.append(goalSec, progressSec, roadmap, now, next, checkSec, ownerSec,
    joinedProblemSection("Open problems", report.open_problems),
    joinedProblemSection("Resolved problems", report.resolved_problems));
  if (report.recovery_action || report.chitra_action) {
    const recovery = el("div", { class: "d-sec" }, el("h5", {}, "Recovery"),
      report.recovery_action ? el("p", {}, el("b", {}, "Action: "), joinedLine(report.recovery_action)) : null,
      report.chitra_action ? el("p", { class: "soft" }, "Chitra: ", joinedLine(report.chitra_action)) : null);
    root.append(recovery);
  }
  return root;
}

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

  if (lane.joined_session) {
    body.append(joinedDetails(lane.joined_session));
  } else {
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
  const cockpit = name === "cockpit";
  $("view-cockpit").hidden = !cockpit;
  $("view-history").hidden = cockpit;
  $("tab-cockpit").classList.toggle("on", cockpit);
  $("tab-history").classList.toggle("on", !cockpit);
  $("tab-cockpit").setAttribute("aria-selected", String(cockpit));
  $("tab-history").setAttribute("aria-selected", String(!cockpit));
}
$("tab-cockpit").addEventListener("click", () => showTab("cockpit"));
$("tab-history").addEventListener("click", () => showTab("history"));

// ------------------------------------------------------------ boot

fetch("/api/state").then((r) => r.json()).then((s) => {
  if (!state) { state = s; renderAll(); }
}).catch(() => { /* SSE will state the truth in the top bar */ });
connect();
