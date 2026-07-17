const state = {
  currentProject: "",
  lastMaxEventId: 0,
  knownConflictKeys: new Set(),
  seededConflicts: false,
  attachedFiles: [],
  logCursors: {},
  logsByJob: {},
  logScrollByJob: {},
  expandedJobs: new Set(),
  contextData: null,
  contextTab: "entries",
  contextEditingId: null,
  contextEditDraft: "",
  contextRequestVersion: 0,
  contextSearch: "",
  contextStateFilter: "all",
  contextDraggingId: null,
  syncedProjects: new Set(),
  dispatchJobs: [],
  dispatchJobsSignature: "",
  dispatchRequestVersion: 0,
  openContextSnapshots: new Set(),
  contextSnapshotCache: {},
  mapFocusCategory: null,
  mapSelectedEntryId: null,
  pendingInteractions: [],
  interactionsSignature: "",
  knownInteractionIds: new Set(),
  telemetryData: null,
  telemetryExplainerSignature: "",
  recategorizeJobs: new Set(),
};

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function shortPath(p) {
  const parts = p.replace(/\\/g, "/").split("/");
  return parts[parts.length - 1] || p;
}

let AGENTS_CACHE = [
  { id: "claude-code", display_name: "Claude Code", capabilities: { dispatch: true } },
  { id: "codex", display_name: "Codex", capabilities: { dispatch: true } },
];

async function loadAgentsCache() {
  try {
    const res = await apiFetch("/api/agents");
    const agents = await res.json();
    if (Array.isArray(agents) && agents.length) AGENTS_CACHE = agents;
  } catch {}
  return AGENTS_CACHE;
}

function dispatchableAgents() {
  return AGENTS_CACHE.filter((a) => a.capabilities?.dispatch !== false);
}

function agentClass(agent) {
  if (agent === "codex") return "codex";
  if (agent === "claude-code") return "claude-code";
  return "local";
}

let noticeTimer = null;

function showToast(text, isConflict) {
  const strip = document.getElementById("notice-strip");
  if (!strip) return;
  strip.textContent = text;
  strip.className = "notice-strip" + (isConflict ? " conflict" : "");
  strip.style.display = "flex";
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => {
    strip.style.display = "none";
  }, 6000);
}

let redirectingToLogin = false;

async function apiFetch(url, opts) {
  let res = await fetch(url, opts);
  if (res.status === 401) {
    await new Promise((r) => setTimeout(r, 400));
    res = await fetch(url, opts);
  }
  if (res.status === 401) {
    if (!redirectingToLogin) {
      redirectingToLogin = true;
      window.location.href = "/";
    }
    throw new Error("unauthenticated");
  }
  return res;
}


async function fetchProjects() {
  const res = await apiFetch("/api/projects");
  const projects = await res.json();
  renderProjects(projects);
}

function renderProjects(projects) {
  const list = document.getElementById("project-list");
  list.innerHTML = "";
  for (const p of projects) {
    const item = el("div", "sidebar-item");
    item.dataset.project = p.project_path;
    if (p.project_path === state.currentProject) item.classList.add("active");
    item.appendChild(el("div", "sidebar-item-title", shortPath(p.project_path)));
    item.appendChild(
      el(
        "div",
        "sidebar-item-meta",
        `${p.event_count} events - ${p.agents.join(", ")}`
      )
    );
    item.addEventListener("click", () => selectProject(p.project_path));
    list.appendChild(item);
  }
  document
    .getElementById("all-projects-item")
    .classList.toggle("active", state.currentProject === "");
}

function selectProject(path) {
  state.currentProject = path;
  state.contextRequestVersion += 1;
  state.contextData = null;
  state.contextEditingId = null;
  state.contextEditDraft = "";
  state.contextSearch = "";
  state.contextStateFilter = "all";
  state.mapFocusCategory = null;
  state.mapSelectedEntryId = null;
  state.telemetryData = null;
  state.telemetryExplainerSignature = "";
  const telemetryExplainer = document.getElementById("telemetry-explainer");
  const telemetryToggle = document.getElementById("telemetry-explain-toggle");
  telemetryExplainer.hidden = true;
  telemetryToggle.setAttribute("aria-expanded", "false");
  telemetryToggle.textContent = "Explain this";
  document.getElementById("context-search").value = "";
  document.getElementById("context-state-filter").value = "all";
  state.lastMaxEventId = 0;
  state.knownConflictKeys = new Set();
  state.seededConflicts = false;
  state.attachedFiles = [];
  state.dispatchJobs = [];
  state.dispatchJobsSignature = "";
  state.dispatchRequestVersion += 1;
  renderAttachedFiles();
  document.getElementById("panel-title").textContent = path ? shortPath(path) : "All projects";
  document.getElementById("telemetry-panel").style.display = path ? "" : "none";
  document.getElementById("context-panel").style.display = path ? "" : "none";
  document.getElementById("dispatch-panel").style.display = path ? "" : "none";
  document.getElementById("dispatch-history-panel").style.display = path ? "" : "none";
  document.getElementById("backfill-btn").style.display = path ? "" : "none";
  document.querySelectorAll(".sidebar-item").forEach((n) => n.classList.remove("active"));
  const match = document.querySelector(`.sidebar-item[data-project="${CSS.escape(path)}"]`);
  if (match) match.classList.add("active");
  else document.getElementById("all-projects-item").classList.add("active");

  pollEvents();
  pollConflicts();
  if (path) {
    loadDispatchHistory();
    syncNativeHistory(path).catch((err) => {
      if (state.currentProject === path) setContextStatus(err.message, true);
    }).finally(() => {
      if (state.currentProject !== path) return;
      loadContext({ showLoading: true });
      loadTelemetry();
    });
  }
}


async function pollEvents() {
  const params = new URLSearchParams({ since_id: String(state.lastMaxEventId), limit: "100" });
  if (state.currentProject) params.set("project", state.currentProject);
  const res = await apiFetch("/api/events?" + params.toString());
  const events = await res.json();

  if (events.length === 0) return;

  const isFirstLoad = state.lastMaxEventId === 0 && !window.__seeded;
  const newMax = Math.max(...events.map((e) => e.id));
  if (newMax > state.lastMaxEventId) state.lastMaxEventId = newMax;

  if (!isFirstLoad) {
    for (const e of events) {
      showToast(`${e.agent}: ${e.summary.slice(0, 120)}`, false);
    }
  }
  window.__seeded = true;

  renderTimeline(events);
  renderAgentChart(events);
  if (state.currentProject && state.contextEditingId === null) {
    loadContext();
  }
}

function renderTimeline(events) {
  const container = document.getElementById("timeline-list");
  if (events.length === 0) {
    container.innerHTML = '<p class="empty">No activity recorded yet.</p>';
    return;
  }
  container.innerHTML = "";
  for (const e of events) {
    const row = el("div", "event-row");
    row.appendChild(el("div", "agent-tag " + agentClass(e.agent), e.agent));
    const body = el("div", "event-body");
    body.appendChild(el("div", "event-summary", e.summary));
    const metaText = state.currentProject
      ? `${e.event_type} - ${e.created_at}`
      : `${shortPath(e.project_path)} - ${e.event_type} - ${e.created_at}`;
    body.appendChild(el("div", "event-meta", metaText));
    row.appendChild(body);
    container.appendChild(row);
  }
}

function renderAgentChart(events) {
  const counts = {};
  for (const e of events) counts[e.agent] = (counts[e.agent] || 0) + 1;
  const max = Math.max(1, ...Object.values(counts));
  const chart = document.getElementById("agent-chart");
  chart.innerHTML = "";
  const agents = Object.keys(counts);
  if (agents.length === 0) {
    chart.appendChild(el("p", "empty", "No activity yet."));
    return;
  }
  for (const agent of agents) {
    const wrap = el("div", "agent-bar");
    const bar = el("div", "bar " + agentClass(agent));
    bar.style.height = Math.max(6, (counts[agent] / max) * 50) + "px";
    wrap.appendChild(bar);
    wrap.appendChild(el("div", "label", `${agent} (${counts[agent]})`));
    chart.appendChild(wrap);
  }
}


async function pollConflicts() {
  const params = new URLSearchParams();
  if (state.currentProject) params.set("project", state.currentProject);
  const res = await apiFetch("/api/conflicts?" + params.toString());
  const conflicts = await res.json();
  renderConflicts(conflicts);

  const wasSeeded = state.seededConflicts;
  state.seededConflicts = true;
  for (const c of conflicts) {
    const key = `${c.file_path}|${c.agent_a}|${c.agent_b}|${c.touched_at_b}`;
    if (!state.knownConflictKeys.has(key)) {
      state.knownConflictKeys.add(key);
      if (wasSeeded) {
        showToast(`Conflict: ${shortPath(c.file_path)} touched by ${c.agent_a} and ${c.agent_b}`, true);
      }
    }
  }
}

function renderConflicts(conflicts) {
  const container = document.getElementById("conflicts-list");
  if (conflicts.length === 0) {
    container.innerHTML = '<p class="empty">No conflicts detected.</p>';
    return;
  }
  container.innerHTML = "";
  for (const c of conflicts) {
    const row = el("div", "conflict-row");
    row.appendChild(el("div", "file", shortPath(c.file_path)));
    const metaText = state.currentProject
      ? `${c.agent_a} then ${c.agent_b} - ${c.touched_at_b}`
      : `${shortPath(c.project_path)} - ${c.agent_a} then ${c.agent_b} - ${c.touched_at_b}`;
    row.appendChild(el("div", "meta", metaText));
    container.appendChild(row);
  }
}


async function loadTelemetry() {
  if (!state.currentProject) return;
  const res = await apiFetch("/api/telemetry?project=" + encodeURIComponent(state.currentProject));
  const data = await res.json();
  state.telemetryData = data;
  renderTelemetry(data);
}

function renderTelemetry(data) {
  const body = document.getElementById("telemetry-body");
  body.innerHTML = "";
  const stats = el("div", "telemetry-stats");

  const stat = (value, label, cls) => {
    const wrap = el("div", "telemetry-stat");
    wrap.appendChild(el("div", "value " + (cls || ""), String(value)));
    wrap.appendChild(el("div", "label", label));
    return wrap;
  };

  const fmt = (n) => (n || 0).toLocaleString();
  stats.appendChild(stat(fmt(data.pooled_source_tokens), "Shared corpus text"));
  stats.appendChild(stat(fmt(data.pooled_context_tokens), "Active shared context", "saved"));
  stats.appendChild(stat(`${data.context_compression_percent || 0}%`, "Corpus compression"));
  stats.appendChild(stat(fmt(data.context_delivered_tokens), "Context delivered"));
  for (const [agentId, tokens] of Object.entries(data.agent_tokens || {})) {
    stats.appendChild(stat(fmt(tokens), `${displayAgent(agentId)} usage`, agentClass(agentId)));
  }
  body.appendChild(stats);

  const parity = el("div", "telemetry-parity");
  const visible = (data.visible_to || []).map(displayAgent).join(" + ");
  parity.appendChild(el(
    "div",
    "telemetry-parity-title",
    `${visible || "All agents"} share one working set · ${fmt(data.exclusive_context_entries)} exclusive entries`,
  ));
  for (const agent of Object.keys(data.delivery_by_agent || {})) {
    const delivery = (data.delivery_by_agent || {})[agent] || {};
    const current = delivery.has_current_context ? "current" : "awaiting current context";
    parity.appendChild(el(
      "div",
      "telemetry-delivery-row",
      `${displayAgent(agent)}: ${fmt(delivery.deliveries)} deliveries / ${fmt(delivery.tokens)} tokens ` +
        `· ${fmt(delivery.tokens_saved)} saved · ${current}`,
    ));
  }
  const deliveryCount = data.context_delivery_count ?? Object.values(data.delivery_by_agent || {})
    .reduce((total, delivery) => total + (delivery.deliveries || 0), 0);
  const baseline = data.baseline_status === "not_established"
    ? "Verified savings: no pooled session yet carries a measured native-usage cost, so no savings are claimed."
    : `Verified savings: ${fmt(data.verified_tokens_saved)} tokens across ${fmt(deliveryCount)} context deliveries ` +
      `(${data.efficiency_gain_percent}% of the cumulative native work represented at delivery time).`;
  parity.appendChild(el("div", "telemetry-baseline", baseline));
  body.appendChild(parity);
  body.appendChild(el("div", "telemetry-methodology", data.methodology));

  if (!document.getElementById("telemetry-explainer").hidden) {
    renderTelemetryExplainer(data);
  }
}

function telemetryMetric(value, label, description) {
  const item = el("article", "economy-metric");
  item.appendChild(el("div", "economy-metric-value", value));
  item.appendChild(el("h4", "economy-metric-label", label));
  item.appendChild(el("p", "economy-metric-copy", description));
  return item;
}

function economyScale(eyebrow, heading, note, rows, fmt) {
  const section = el("section", "economy-scale");
  const head = el("div", "economy-section-heading");
  head.appendChild(el("span", "economy-eyebrow", eyebrow));
  head.appendChild(el("h3", "", heading));
  section.appendChild(head);
  if (note) section.appendChild(el("p", "economy-scale-note", note));
  const max = Math.max(1, ...rows.map((row) => row.value || 0));
  rows.forEach((row, index) => {
    const item = el("div", "economy-scale-row");
    item.style.setProperty("--scale", String(index));
    const rowHead = el("div", "economy-scale-row-head");
    rowHead.appendChild(el("span", "economy-scale-row-label", row.label));
    rowHead.appendChild(el("span", "economy-scale-row-value", fmt(row.value || 0)));
    item.appendChild(rowHead);
    const track = el("div", "economy-scale-track");
    const bar = el("span", "economy-scale-bar" + (row.highlight ? " is-highlight" : ""));
    bar.style.setProperty("--scale-width", `${((row.value || 0) / max) * 100}%`);
    track.appendChild(bar);
    item.appendChild(track);
    section.appendChild(item);
  });
  return section;
}

function restartTelemetryAnimation() {
  const explainer = document.getElementById("telemetry-explainer");
  explainer.classList.remove("is-animating");
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (!explainer.hidden) explainer.classList.add("is-animating");
  }));
}

function renderTelemetryExplainer(data, restart = false) {
  const body = document.getElementById("telemetry-explainer-body");
  const fmt = (n) => (n || 0).toLocaleString();
  const deliveries = data.context_delivery_count ?? Object.values(data.delivery_by_agent || {})
    .reduce((total, delivery) => total + (delivery.deliveries || 0), 0);
  const verified = data.baseline_status === "measured";
  const signature = JSON.stringify([
    data.pooled_source_tokens, data.pooled_context_tokens, data.context_compression_percent,
    data.context_delivered_tokens, data.claude_tokens, data.codex_tokens,
    data.delivery_native_tokens, data.verified_tokens_saved, data.efficiency_gain_percent,
    deliveries, data.delivery_by_agent,
  ]);
  if (!restart && body.childElementCount && signature === state.telemetryExplainerSignature) return;
  state.telemetryExplainerSignature = signature;
  if (!restart) document.getElementById("telemetry-explainer").classList.remove("is-animating");
  body.replaceChildren();

  const intro = el("div", "economy-intro");
  const introCopy = el("div", "economy-intro-copy");
  introCopy.appendChild(el("span", "economy-eyebrow", "From past work to the next agent"));
  introCopy.appendChild(el("h3", "", "How the shared context system actually works"));
  introCopy.appendChild(el(
    "p",
    "",
    "AgentMemorySync keeps provider history as evidence, curates a much smaller common briefing, and gives that same briefing to Claude Code and Codex. The numbers below follow that path.",
  ));
  intro.appendChild(introCopy);
  const introActions = el("div", "economy-intro-actions");
  const replay = el("button", "telemetry-explainer-action", "Replay animation");
  replay.type = "button";
  replay.addEventListener("click", restartTelemetryAnimation);
  introActions.appendChild(replay);
  const close = el("button", "telemetry-explainer-action", "Close");
  close.type = "button";
  close.addEventListener("click", () => toggleTelemetryExplainer(false));
  introActions.appendChild(close);
  intro.appendChild(introActions);
  body.appendChild(intro);

  const flow = el("div", "economy-flow");
  const flowSteps = [
    {
      number: "01", verb: "Capture", value: fmt(data.pooled_source_tokens), unit: "estimated text tokens",
      copy: "Included session text and project knowledge form the shared corpus. This is retained evidence, not the prompt sent on every run.",
    },
    {
      number: "02", verb: "Curate", value: fmt(data.pooled_context_tokens), unit: "active briefing tokens",
      copy: `Pinned plus recent findings become one agent-neutral briefing. It is ${data.context_compression_percent || 0}% smaller than the corpus by text size.`,
    },
    {
      number: "03", verb: "Deliver", value: fmt(data.context_delivered_tokens), unit: `tokens across ${fmt(deliveries)} deliveries`,
      copy: "Each context injection is recorded. This total grows whenever a new Claude Code or Codex session receives a briefing.",
    },
    {
      number: "04", verb: "Reuse", value: verified ? fmt(data.verified_tokens_saved) : "Pending", unit: "verified reusable-work gap",
      copy: verified
        ? "For each delivery, the compact briefing is compared with the measured native work represented by its active entries."
        : "Savings stay unclaimed until a delivered pooled session has a measured native token cost.",
    },
  ];
  flowSteps.forEach((step, index) => {
    const card = el("article", "economy-flow-step");
    card.style.setProperty("--step", String(index));
    const top = el("div", "economy-flow-top");
    top.appendChild(el("span", "economy-step-number", step.number));
    top.appendChild(el("span", "economy-step-verb", step.verb));
    card.appendChild(top);
    card.appendChild(el("strong", "economy-step-value", step.value));
    card.appendChild(el("span", "economy-step-unit", step.unit));
    card.appendChild(el("p", "", step.copy));
    flow.appendChild(card);
  });
  body.appendChild(flow);

  const reuse = el("div", "economy-reuse");
  const reuseExplanation = el("section", "economy-reuse-copy");
  reuseExplanation.appendChild(el("span", "economy-eyebrow", "Why can savings become so large?"));
  reuseExplanation.appendChild(el("h3", "", "The gap is counted once per real delivery"));
  reuseExplanation.appendChild(el(
    "p",
    "",
    `There are ${fmt(deliveries)} recorded deliveries. The same expensive investigation can help several later sessions, so each delivery contributes its own measured gap. This is cumulative reuse, not an arbitrary corpus multiplier and not a claim that a provider refunded these tokens.`,
  ));
  const formula = el("div", "economy-formula");
  formula.appendChild(el("span", "economy-formula-label", "Per-delivery formula"));
  formula.appendChild(el("code", "", "SUM max(0, native work[i] - briefing[i])"));
  reuseExplanation.appendChild(formula);
  reuse.appendChild(reuseExplanation);

  const totals = el("section", "economy-totals");
  totals.appendChild(el("span", "economy-eyebrow", verified ? "Your measured totals" : "Baseline not established"));
  totals.appendChild(telemetryMetric(
    fmt(data.delivery_native_tokens),
    "Native work represented",
    "Sum of active entries' native session costs, snapshotted separately at every delivery.",
  ));
  totals.appendChild(telemetryMetric(
    fmt(data.context_delivered_tokens),
    "Compact context sent",
    "Sum of the smaller briefing payloads actually injected into agent sessions.",
  ));
  totals.appendChild(telemetryMetric(
    verified ? fmt(data.verified_tokens_saved) : "Not claimed",
    "Verified reuse gap",
    verified
      ? `${data.efficiency_gain_percent}% of the cumulative native baseline across recorded deliveries.`
      : "A real native-usage snapshot is required before this system reports savings.",
  ));
  reuse.appendChild(totals);
  body.appendChild(reuse);

  const lanes = el("section", "economy-lanes");
  lanes.appendChild(el("h3", "", "Where deliveries went"));
  const deliveryRows = Object.keys(data.delivery_by_agent || {}).map((id) => [id, displayAgent(id)]);
  const maxSaved = Math.max(1, ...deliveryRows.map(([id]) => ((data.delivery_by_agent || {})[id] || {}).tokens_saved || 0));
  deliveryRows.forEach(([id, label], index) => {
    const delivery = (data.delivery_by_agent || {})[id] || {};
    const row = el("div", "economy-lane");
    row.style.setProperty("--lane", String(index));
    const laneHead = el("div", "economy-lane-head");
    laneHead.appendChild(el("strong", "agent-tag " + agentClass(id), label));
    laneHead.appendChild(el(
      "span",
      "",
      `${fmt(delivery.deliveries)} deliveries - ${fmt(delivery.tokens)} sent - ${fmt(delivery.tokens_saved)} saved`,
    ));
    row.appendChild(laneHead);
    const track = el("div", "economy-lane-track");
    const bar = el("span", "economy-lane-bar " + agentClass(id));
    bar.style.setProperty("--lane-width", `${((delivery.tokens_saved || 0) / maxSaved) * 100}%`);
    track.appendChild(bar);
    row.appendChild(track);
    lanes.appendChild(row);
  });
  body.appendChild(lanes);

  const glossary = el("section", "economy-glossary");
  const glossaryHead = el("div", "economy-section-heading");
  glossaryHead.appendChild(el("span", "economy-eyebrow", "Read every dashboard metric"));
  glossaryHead.appendChild(el("h3", "", "What each number means"));
  glossary.appendChild(glossaryHead);
  const metricGrid = el("div", "economy-metric-grid");
  [
    [fmt(data.pooled_source_tokens), "Shared corpus text", "Estimated token size of all included source text retained in the pool, including older material outside the active window."],
    [fmt(data.pooled_context_tokens), "Active shared context", "Estimated size of the exact pinned-plus-recent briefing prepared for the next context injection."],
    [`${data.context_compression_percent || 0}%`, "Corpus compression", "Text-size reduction from the full included corpus to the active briefing: 1 - active / corpus. It is not a savings claim."],
    [fmt(data.context_delivered_tokens), "Context delivered", "Cumulative estimated briefing tokens actually injected across all recorded agent sessions."],
    [fmt(data.claude_tokens), "Claude Code usage", "Native token usage read from Claude Code history for this repository. It measures agent activity, not briefing size."],
    [fmt(data.codex_tokens), "Codex usage", "Native token usage read from Codex history for this repository. It measures agent activity, not briefing size."],
  ].forEach(([value, label, description]) => metricGrid.appendChild(telemetryMetric(value, label, description)));
  glossary.appendChild(metricGrid);
  body.appendChild(glossary);

  const boundary = el("aside", "economy-boundary");
  boundary.appendChild(el("strong", "", "Measurement boundary"));
  boundary.appendChild(el(
    "span",
    "",
    "Provider usage and delivery events are measured from local histories. Corpus and briefing sizes use a four-characters-per-token text estimate. Verified savings compare two recorded quantities per delivery; they are not an A/B control run, a billing counter, or proof that an agent would have reread every native token.",
  ));
  body.appendChild(boundary);

  if (restart) restartTelemetryAnimation();
}

function toggleTelemetryExplainer(forceOpen) {
  const explainer = document.getElementById("telemetry-explainer");
  const toggle = document.getElementById("telemetry-explain-toggle");
  const open = forceOpen === undefined ? explainer.hidden : forceOpen;
  explainer.hidden = !open;
  toggle.setAttribute("aria-expanded", String(open));
  toggle.textContent = open ? "Close explainer" : "Explain this";
  if (open && state.telemetryData) renderTelemetryExplainer(state.telemetryData, true);
  if (!open) explainer.classList.remove("is-animating");
}


function setContextStatus(message, isError = false) {
  const status = document.getElementById("context-status");
  status.textContent = message;
  status.className = "context-status" + (isError ? " error" : "");
}

async function responseJson(res, fallback) {
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) throw new Error(data.detail || fallback);
  return data;
}

async function loadContext({ showLoading = false } = {}) {
  const project = state.currentProject;
  if (!project || state.contextEditingId !== null) return;
  const version = ++state.contextRequestVersion;
  if (showLoading && !state.contextData) {
    setContextStatus("Loading pooled context...");
    document.getElementById("context-entries").replaceChildren();
  }
  try {
    const res = await apiFetch("/api/context?project=" + encodeURIComponent(project));
    const data = await responseJson(res, "Failed to load pooled context.");
    if (project !== state.currentProject || version !== state.contextRequestVersion) return;
    state.contextData = data;
    setContextStatus("");
    renderContext();
  } catch (err) {
    if (project !== state.currentProject || version !== state.contextRequestVersion) return;
    setContextStatus(err.message + " Select Retry to try again.", true);
    const retry = el("button", "btn btn-secondary btn-tiny", "Retry");
    retry.addEventListener("click", () => loadContext({ showLoading: true }));
    document.getElementById("context-entries").replaceChildren(retry);
  }
}

async function mutateContext(url, options, fallback) {
  const project = state.currentProject;
  const version = ++state.contextRequestVersion;
  setContextStatus("Saving...");
  try {
    const res = await apiFetch(url, options);
    const data = await responseJson(res, fallback);
    if (project !== state.currentProject || version !== state.contextRequestVersion) return false;
    state.contextData = data;
    state.contextEditingId = null;
    state.contextEditDraft = "";
    setContextStatus("");
    renderContext();
    return true;
  } catch (err) {
    if (project === state.currentProject && version === state.contextRequestVersion) {
      setContextStatus(err.message, true);
    }
    return false;
  }
}

function contextPatch(eventId, patch) {
  return mutateContext(
    `/api/context/events/${eventId}?project=${encodeURIComponent(state.currentProject)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    },
    "Failed to update context entry."
  );
}

function contextButton(text, action, title) {
  const button = el("button", "context-entry-action", text);
  button.type = "button";
  if (title) button.title = title;
  button.addEventListener("click", action);
  return button;
}

const CONTEXT_CATEGORY_META = {
  decision: {
    label: "Decisions",
    description: "Settled direction agents should preserve unless requirements change.",
    use: "Use before choosing an approach so settled tradeoffs are not reopened.",
  },
  constraint: {
    label: "Constraints",
    description: "Hard requirements, blockers, safety boundaries, and environment limits.",
    use: "Apply before editing, testing, or deploying so the agent stays inside known boundaries.",
  },
  task: {
    label: "Open work",
    description: "Unfinished work, blockers, and explicit next steps another agent can continue.",
    use: "Use to resume work without repeating investigation or losing the next action.",
  },
  artifact: {
    label: "Delivered changes",
    description: "Implemented behavior, changed files, test results, and produced artifacts.",
    use: "Use to locate existing implementation and avoid duplicating completed work.",
  },
  insight: {
    label: "Findings",
    description: "Verified research, root causes, failed approaches, and reusable lessons.",
    use: "Use when diagnosing related work so evidence and failed paths are not rediscovered.",
  },
  note: {
    label: "Project knowledge",
    description: "Developer-authored facts and instructions that are not obvious from the code.",
    use: "Use as durable repository guidance across every agent and task.",
  },
  activity: {
    label: "Activity records",
    description: "General work records that have not yet been turned into actionable knowledge.",
    use: "Keep only when the work result changes what a future agent should do.",
  },
};

const CONTEXT_EVENT_META = {
  history: { label: "Imported past session", meaning: "Recovered from a provider's native history." },
  turn: { label: "Agent task result", meaning: "Captured when an interactive agent turn ended." },
  dispatch: { label: "Dashboard deployment result", meaning: "Captured from an agent launched by this dashboard." },
  handoff: { label: "Handoff created", meaning: "Points to a handoff prepared for another agent." },
  context_note: { label: "Developer briefing", meaning: "Written directly by a developer for future agents." },
  note: { label: "Agent note", meaning: "Recorded explicitly by an agent or integration." },
};

function displayAgent(agent) {
  if (agent === "developer" || agent === "user") return "Developer";
  const known = AGENTS_CACHE.find((a) => a.id === agent);
  if (known) return known.display_name;
  if (agent === "claude-code") return "Claude Code";
  if (agent === "codex") return "Codex";
  return String(agent || "Unknown source");
}

function compactText(value, limit = 110) {
  const text = String(value || "")
    .replace(/^[\s#>*`-]+/gm, "")
    .replace(/\s+/g, " ")
    .trim();
  if (text.length <= limit) return text;
  return text.slice(0, limit - 1).trimEnd() + "…";
}

function firstUsefulLine(value) {
  const lines = String(value || "").split(/\r?\n/);
  for (const raw of lines) {
    const line = raw.replace(/^[\s#>*`-]+/, "").trim();
    if (!line || /^(summary|result|outcome|done|conclusion):?$/i.test(line)) continue;
    return compactText(line);
  }
  return compactText(value) || "Untitled context record";
}

function splitHistorySummary(summary) {
  const match = String(summary || "").match(/^Asked:\s*([\s\S]*?)\s*\|\s*Result:\s*([\s\S]*)$/i);
  if (!match) return null;
  return { request: match[1].trim(), outcome: match[2].trim() };
}

function contextEntryPresentation(entry) {
  const summary = String(entry.effective_summary || "").trim();
  const history = entry.event_type === "history" ? splitHistorySummary(summary) : null;
  const eventMeta = CONTEXT_EVENT_META[entry.event_type] || {
    label: String(entry.event_type || "record").replaceAll("_", " "),
    meaning: "Captured by an AgentMemorySync integration.",
  };
  const headlineSource = history?.request || summary;
  const outcome = history?.outcome || summary;
  const generic = /^(done|work|worked|did a thing|real work|hello|x|event \d+|activity \d+)\.?$/i.test(summary);
  const needsWork = !summary || summary.length < 45 || generic || (history && history.outcome.length < 55);
  return {
    eventMeta,
    history,
    headline: firstUsefulLine(headlineSource),
    outcome,
    needsWork,
    qualityReason: needsWork
      ? "Too little concrete detail to guide another agent. Rewrite it with the change, reason, and consequence."
      : "Contains enough detail to give another agent a useful starting point.",
    use: CONTEXT_CATEGORY_META[entry.category]?.use || CONTEXT_CATEGORY_META.activity.use,
  };
}

function formatContextTime(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return { relative: String(value || "Unknown time"), full: String(value || "") };
  const seconds = Math.round((parsed.getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const ranges = [
    [31536000, "year"], [2592000, "month"], [604800, "week"],
    [86400, "day"], [3600, "hour"], [60, "minute"],
  ];
  for (const [size, unit] of ranges) {
    if (Math.abs(seconds) >= size) {
      return { relative: formatter.format(Math.round(seconds / size), unit), full: parsed.toLocaleString() };
    }
  }
  return { relative: formatter.format(seconds, "second"), full: parsed.toLocaleString() };
}

function contextSelectedIds() {
  if (!state.contextData) return new Set();
  return new Set([...state.contextData.pinned, ...state.contextData.recent].map((entry) => entry.id));
}

function contextEntryState(entry, selectedIds) {
  if (!entry.included) return {
    key: "excluded",
    label: "Not sent",
    explanation: "Removed from the shared briefing. The original record remains here for audit and can be restored.",
  };
  if (entry.pinned) return {
    key: "pinned",
    label: "Sent every run",
    explanation: "Delivered to both agents on every run, regardless of age or the newest-entry limit.",
  };
  if (selectedIds.has(entry.id)) return {
    key: "selected",
    label: "Sent next run",
    explanation: `Delivered to both agents because it is among the ${state.contextData.settings.recent_limit} newest included records.`,
  };
  return {
    key: "outside",
    label: "Stored, not sent",
    explanation: "Still included in the corpus, but too old for the current newest-entry limit. Keep it every run or raise the limit to deliver it.",
  };
}

function contextEntryMatches(entry, selectedIds) {
  const stateInfo = contextEntryState(entry, selectedIds);
  const presentation = contextEntryPresentation(entry);
  const stateFilter = state.contextStateFilter;
  if (stateFilter === "selected" && !selectedIds.has(entry.id)) return false;
  if (stateFilter === "needs_work" && !presentation.needsWork) return false;
  if (!["all", "selected", "needs_work"].includes(stateFilter) && stateInfo.key !== stateFilter) return false;
  const query = state.contextSearch.trim().toLowerCase();
  if (!query) return true;
  return [entry.effective_summary, entry.summary, entry.agent, entry.event_type, entry.category,
    presentation.headline, presentation.use, presentation.eventMeta.label]
    .some((value) => String(value || "").toLowerCase().includes(query));
}

function contextCategorySelect(entry) {
  const select = el("select", "context-category-select");
  select.setAttribute("aria-label", `Dashboard category for ${contextEntryPresentation(entry).headline}`);
  select.title = "Organizes this dashboard only; it does not change selection or agent delivery.";
  for (const category of state.contextData.categories) {
    const option = new Option(CONTEXT_CATEGORY_META[category].label.replace(/s$/, ""), category);
    select.appendChild(option);
  }
  select.value = entry.category;
  select.addEventListener("change", () => contextPatch(entry.id, { category: select.value }));
  select.addEventListener("click", (event) => event.stopPropagation());
  return select;
}

function appendContextText(container, text, { label = "What happened", limit = 720 } = {}) {
  const block = el("div", "context-content-block");
  block.appendChild(el("div", "context-content-label", label));
  const value = String(text || "No useful result was captured.");
  if (value.length <= limit) {
    block.appendChild(el("div", "context-content-value", value));
  } else {
    block.appendChild(el("div", "context-content-value", value.slice(0, limit).trimEnd() + "…"));
    const details = el("details", "context-inline-details");
    details.appendChild(el("summary", "", `Read full text (${Math.ceil(value.length / 4).toLocaleString()} estimated tokens)`));
    details.appendChild(el("pre", "context-entry-full", value));
    block.appendChild(details);
  }
  container.appendChild(block);
}

function renderContextEntry(entry, selectedIds) {
  const card = el("article", "context-entry");
  card.dataset.entryId = String(entry.id);
  card.dataset.category = entry.category;
  if (entry.pinned) card.classList.add("pinned");
  if (!entry.included) card.classList.add("excluded");
  const stateInfo = contextEntryState(entry, selectedIds);
  const presentation = contextEntryPresentation(entry);
  card.classList.add("state-" + stateInfo.key);

  if (state.contextEditingId !== entry.id) {
    card.draggable = true;
    card.addEventListener("dragstart", (event) => {
      state.contextDraggingId = entry.id;
      card.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", String(entry.id));
    });
    card.addEventListener("dragend", () => {
      state.contextDraggingId = null;
      card.classList.remove("dragging");
      document.querySelectorAll(".context-category-section.drag-over")
        .forEach((section) => section.classList.remove("drag-over"));
    });
  }

  const head = el("div", "context-entry-head");
  const handle = el("span", "context-drag-handle", "⋮⋮");
  handle.title = "Drag to organize this record. Category changes do not affect agent delivery.";
  head.appendChild(handle);
  head.appendChild(el("span", "agent-tag " + agentClass(entry.agent), displayAgent(entry.agent)));
  const sourceType = el("span", "context-entry-type", presentation.eventMeta.label);
  sourceType.title = presentation.eventMeta.meaning;
  head.appendChild(sourceType);
  head.appendChild(el("span", "context-state-badge " + stateInfo.key, stateInfo.label));
  const promptTokens = Math.ceil(String(entry.effective_summary || "").length / 4);
  const cost = el("span", "context-entry-cost", `~${promptTokens.toLocaleString()} tokens`);
  cost.title = "Estimated prompt cost when this record is delivered. This is based on the agent-facing text, not native model usage.";
  head.appendChild(cost);
  const captured = formatContextTime(entry.created_at);
  const time = el("time", "context-entry-time", captured.relative);
  time.dateTime = entry.created_at;
  time.title = `Captured ${captured.full}`;
  head.appendChild(time);
  card.appendChild(head);

  if (state.contextEditingId === entry.id) {
    const editHeading = el("div", "context-edit-heading");
    editHeading.appendChild(el("strong", "", "Rewrite the text both agents receive"));
    editHeading.appendChild(el("span", "", "State what changed, why it matters, and the next action. The original audit record stays unchanged."));
    card.appendChild(editHeading);
    const textarea = el("textarea", "context-editor");
    textarea.maxLength = 2000;
    textarea.value = state.contextEditDraft;
    textarea.setAttribute("aria-label", "Agent-facing briefing text");
    textarea.addEventListener("input", () => { state.contextEditDraft = textarea.value; });
    card.appendChild(textarea);
    const editActions = el("div", "context-entry-actions context-edit-actions");
    editActions.appendChild(contextButton("Cancel", () => {
      state.contextEditingId = null;
      state.contextEditDraft = "";
      renderContext();
    }));
    editActions.appendChild(contextButton("Save agent text", () => contextPatch(entry.id, {
      context_summary: state.contextEditDraft,
    })));
    card.appendChild(editActions);
    queueMicrotask(() => textarea.focus());
    return card;
  }

  const titleRow = el("div", "context-title-row");
  titleRow.appendChild(el("h4", "context-entry-title", presentation.headline));
  const quality = el(
    "span",
    "context-quality-badge " + (presentation.needsWork ? "needs-work" : "useful"),
    presentation.needsWork ? "Needs rewrite" : "Useful detail",
  );
  quality.title = presentation.qualityReason;
  titleRow.appendChild(quality);
  card.appendChild(titleRow);

  const delivery = el("div", "context-delivery-line " + stateInfo.key);
  delivery.appendChild(el("strong", "", stateInfo.label));
  delivery.appendChild(el("span", "", stateInfo.explanation));
  card.appendChild(delivery);

  if (presentation.history?.request) {
    appendContextText(card, presentation.history.request, { label: "Original task", limit: 360 });
  }
  appendContextText(card, presentation.outcome, {
    label: presentation.history ? "Result captured" : "What happened",
  });

  const useBlock = el("div", "context-use-block");
  useBlock.appendChild(el("span", "context-content-label", "When this helps"));
  useBlock.appendChild(el("span", "", presentation.use));
  card.appendChild(useBlock);

  if (presentation.needsWork) {
    const warning = el("div", "context-quality-warning");
    warning.appendChild(el("strong", "", "Why this needs attention: "));
    warning.appendChild(document.createTextNode(presentation.qualityReason));
    card.appendChild(warning);
  }

  if (entry.context_summary !== null) {
    const audit = el("details", "context-audit-details");
    audit.appendChild(el("summary", "", "Compare with original captured record"));
    audit.appendChild(el("pre", "context-entry-full", entry.summary || "No original text was captured."));
    card.appendChild(audit);
    card.appendChild(el("div", "context-overlay-label", "Agent text rewritten · original audit record preserved"));
  }

  const actions = el("div", "context-entry-actions");
  const categoryControl = el("label", "context-category-control", "Organize as");
  categoryControl.appendChild(contextCategorySelect(entry));
  categoryControl.title = "Dashboard organization only. Category does not change what agents receive.";
  actions.appendChild(categoryControl);
  if (entry.category_source === "manual") {
    actions.appendChild(contextButton("Restore suggested category", () => {
      contextPatch(entry.id, { reset_category: true });
    }, "Remove the developer override and infer the category from the event."));
  }
  actions.appendChild(contextButton(entry.included ? "Remove from briefing" : "Restore to briefing", () => {
    contextPatch(entry.id, { included: !entry.included });
  }, entry.included
    ? "Send to neither agent while retaining the original record here."
    : "Make the record eligible for delivery to both agents again."));
  if (entry.included) {
    actions.appendChild(contextButton(entry.pinned ? "Use newest-entry rule" : "Keep on every run", () => {
      contextPatch(entry.id, { pinned: !entry.pinned });
    }, entry.pinned
      ? "Stop always sending this record; its age will determine whether it is delivered."
      : "Always send this record to both agents, regardless of its age."));
  } else {
    actions.appendChild(contextButton("Restore and keep every run", () => {
      contextPatch(entry.id, { included: true, pinned: true });
    }, "Restore the record and always send it to both agents."));
  }
  actions.appendChild(contextButton("Rewrite agent text", () => {
    state.contextEditingId = entry.id;
    state.contextEditDraft = entry.effective_summary.slice(0, 2000);
    renderContext();
  }, "Improve the concise briefing without changing the captured audit record."));
  if (entry.context_summary !== null) {
    actions.appendChild(contextButton("Restore captured text", () => contextPatch(entry.id, { reset_summary: true }),
      "Discard the rewritten agent text and deliver the original captured text again."));
  }
  if (entry.event_type === "context_note") {
    actions.appendChild(contextButton("Delete briefing", () => {
      if (!window.confirm("Permanently delete this developer-authored briefing?")) return;
      mutateContext(
        `/api/context/notes/${entry.id}?project=${encodeURIComponent(state.currentProject)}`,
        { method: "DELETE" },
        "Failed to delete project briefing."
      );
    }));
  }
  card.appendChild(actions);
  return card;
}

function appendContextCategory(container, category, entries, selectedIds) {
  const meta = CONTEXT_CATEGORY_META[category];
  const section = el("section", "context-section context-category-section category-" + category);
  section.dataset.category = category;
  const heading = el("h3", "context-section-title");
  heading.appendChild(el("span", "context-category-dot"));
  heading.appendChild(document.createTextNode(meta.label));
  heading.appendChild(el("span", "context-section-count", String(entries.length)));
  section.appendChild(heading);
  section.appendChild(el("p", "context-category-description", meta.description));
  section.addEventListener("dragover", (event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    section.classList.add("drag-over");
  });
  section.addEventListener("dragleave", (event) => {
    if (!section.contains(event.relatedTarget)) section.classList.remove("drag-over");
  });
  section.addEventListener("drop", (event) => {
    event.preventDefault();
    section.classList.remove("drag-over");
    const eventId = Number(event.dataTransfer.getData("text/plain") || state.contextDraggingId);
    const entry = state.contextData.entries.find((item) => item.id === eventId);
    if (entry && entry.category !== category) contextPatch(eventId, { category });
  });
  if (!entries.length) {
    section.appendChild(el("p", "context-drop-empty", "Drop an entry here"));
  } else {
    for (const entry of entries) section.appendChild(renderContextEntry(entry, selectedIds));
  }
  container.appendChild(section);
}

function appendBreakdownChart(container, title, rows) {
  const chart = el("section", "context-chart");
  chart.appendChild(el("h3", "context-map-title", title));
  const max = Math.max(1, ...rows.map((row) => row.value));
  for (const row of rows) {
    const line = el("div", "context-chart-row");
    line.appendChild(el("span", "context-chart-label", row.label));
    const track = el("div", "context-chart-track");
    const bar = el("span", "context-chart-bar");
    bar.style.width = `${(row.value / max) * 100}%`;
    bar.title = `${row.label}: ${row.value}`;
    track.appendChild(bar);
    line.appendChild(track);
    line.appendChild(el("span", "context-chart-value", String(row.value)));
    chart.appendChild(line);
  }
  container.appendChild(chart);
}

function renderContextGraph(container, data, selectedIds) {
  const graph = el("section", "context-graph");
  const focus = state.mapFocusCategory;

  const crumb = el("nav", "context-map-crumb");
  const rootCrumb = el("button", "context-map-crumb-btn" + (focus ? "" : " active"), "Shared briefing");
  rootCrumb.type = "button";
  rootCrumb.addEventListener("click", () => {
    state.mapFocusCategory = null;
    state.mapSelectedEntryId = null;
    renderContextMap();
  });
  crumb.appendChild(rootCrumb);
  if (focus) {
    crumb.appendChild(el("span", "context-map-crumb-sep", "›"));
    crumb.appendChild(el("span", "context-map-crumb-btn active", CONTEXT_CATEGORY_META[focus].label));
  }
  graph.appendChild(crumb);

  const entries = focus
    ? data.entries.filter((entry) => entry.category === focus).slice().reverse()
    : data.entries.filter((entry) => selectedIds.has(entry.id)).reverse();

  graph.appendChild(el(
    "h3",
    "context-map-title",
    focus ? `${CONTEXT_CATEGORY_META[focus].label}: every record` : "Next-run delivery path"
  ));
  graph.appendChild(el(
    "p",
    "context-map-copy",
    focus
      ? `${CONTEXT_CATEGORY_META[focus].description}. Click a record to view or edit it in place; click the category again to go back.`
      : "Click a category to browse every record in it. Click a record to view or edit it in place."
  ));
  if (!entries.length) {
    graph.appendChild(el("p", "empty", focus ? "No records in this category yet." : "No entries are currently selected for agent context."));
    container.appendChild(graph);
    return;
  }

  const visibleEntries = entries.slice(-12);
  const categories = focus ? [focus] : [...new Set(visibleEntries.map((entry) => entry.category))];
  const width = 900;
  const height = Math.max(260, visibleEntries.length * 58 + 40);
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("class", "context-graph-svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Graph of pooled context grouped by category");
  const makeSvg = (tag, attrs, text) => {
    const node = document.createElementNS(svgNS, tag);
    for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
    if (text !== undefined) node.textContent = text;
    svg.appendChild(node);
    return node;
  };
  const addTip = (node, text) => {
    const tip = document.createElementNS(svgNS, "title");
    tip.textContent = text;
    node.appendChild(tip);
  };
  const rootY = height / 2;
  const rootRect = makeSvg("rect", { x: 15, y: rootY - 22, width: 145, height: 44, rx: 10, class: "graph-root" });
  makeSvg("text", { x: 87, y: rootY + 5, class: "graph-label", "text-anchor": "middle" }, "Shared briefing");
  if (focus) {
    rootRect.classList.add("graph-node");
    rootRect.addEventListener("click", () => {
      state.mapFocusCategory = null;
      state.mapSelectedEntryId = null;
      renderContextMap();
    });
    addTip(rootRect, "Back to every category currently delivered to agents.");
  }

  const categoryPositions = {};
  categories.forEach((category, index) => {
    const y = ((index + 1) * height) / (categories.length + 1);
    categoryPositions[category] = y;
    makeSvg("path", { d: `M160 ${rootY} C210 ${rootY}, 210 ${y}, 260 ${y}`, class: "graph-edge" });
    const catRect = makeSvg("rect", {
      x: 260, y: y - 19, width: 135, height: 38, rx: 8,
      class: `graph-category graph-node category-${category}${category === focus ? " focused" : ""}`,
    });
    catRect.addEventListener("click", () => {
      state.mapFocusCategory = state.mapFocusCategory === category ? null : category;
      state.mapSelectedEntryId = null;
      renderContextMap();
    });
    addTip(
      catRect,
      `${CONTEXT_CATEGORY_META[category].label}: ${CONTEXT_CATEGORY_META[category].description}. ` +
        `Click to ${category === focus ? "collapse back to the overview" : "browse every record in this category"}.`
    );
    makeSvg("text", { x: 327, y: y + 5, class: "graph-label", "text-anchor": "middle" }, CONTEXT_CATEGORY_META[category].label);
  });
  visibleEntries.forEach((entry, index) => {
    const y = 35 + index * ((height - 70) / Math.max(1, visibleEntries.length - 1));
    const categoryY = categoryPositions[entry.category];
    makeSvg("path", { d: `M395 ${categoryY} C445 ${categoryY}, 445 ${y}, 495 ${y}`, class: "graph-edge" });
    const presentation = contextEntryPresentation(entry);
    const entryRect = makeSvg("rect", {
      x: 495, y: y - 20, width: 385, height: 40, rx: 8,
      class: "graph-entry graph-node" + (entry.id === state.mapSelectedEntryId ? " selected" : ""),
    });
    entryRect.addEventListener("click", () => {
      state.mapSelectedEntryId = state.mapSelectedEntryId === entry.id ? null : entry.id;
      renderContextMap();
    });
    addTip(
      entryRect,
      `${presentation.headline}\n\n${presentation.outcome}\n\nWhen this helps: ${presentation.use}\n\n` +
        `${displayAgent(entry.agent)} · ${presentation.eventMeta.label} · ${formatContextTime(entry.created_at).full}`
    );
    makeSvg("text", { x: 510, y: y - 3, class: "graph-entry-label" }, presentation.headline.slice(0, 58));
    makeSvg("text", { x: 510, y: y + 12, class: "graph-entry-meta" }, `${displayAgent(entry.agent)} · ${presentation.eventMeta.label}`);
  });
  graph.appendChild(svg);
  if (entries.length > visibleEntries.length) {
    graph.appendChild(el(
      "p",
      "context-map-copy",
      `Showing the newest ${visibleEntries.length} of ${entries.length}${focus ? " records in this category" : " selected entries"}.`
    ));
  }

  if (state.mapSelectedEntryId) {
    const selectedEntry = data.entries.find((entry) => entry.id === state.mapSelectedEntryId);
    if (selectedEntry) {
      const detail = el("div", "context-map-detail");
      detail.appendChild(el("h4", "context-map-detail-title", "Selected record"));
      detail.appendChild(renderContextEntry(selectedEntry, selectedIds));
      graph.appendChild(detail);
    } else {
      state.mapSelectedEntryId = null;
    }
  }
  container.appendChild(graph);
}

function renderContextMap() {
  const data = state.contextData;
  const container = document.getElementById("context-map");
  container.replaceChildren();
  const selectedIds = contextSelectedIds();
  const selected = data.entries.filter((entry) => selectedIds.has(entry.id)).reverse();
  const charts = el("div", "context-chart-grid");
  const categoryRows = data.categories.map((category) => ({
    label: CONTEXT_CATEGORY_META[category].label,
    value: selected.filter((entry) => entry.category === category).length,
  }));
  const agents = [...new Set(selected.map((entry) => entry.agent))];
  const agentRows = agents.map((agent) => ({
    label: displayAgent(agent),
    value: selected.filter((entry) => entry.agent === agent).length,
  }));
  const qualityRows = [
    { label: "Useful detail", value: selected.filter((entry) => !contextEntryPresentation(entry).needsWork).length },
    { label: "Needs rewrite", value: selected.filter((entry) => contextEntryPresentation(entry).needsWork).length },
  ];
  appendBreakdownChart(charts, "Next-run entries by purpose", categoryRows);
  appendBreakdownChart(charts, "Next-run entries by source", agentRows);
  appendBreakdownChart(charts, "Briefing readiness", qualityRows);
  container.appendChild(charts);
  renderContextGraph(container, data, selectedIds);
}

function renderContext() {
  const data = state.contextData;
  if (!data) return;
  const selectedIds = contextSelectedIds();
  const selectedEntries = data.entries.filter((entry) => selectedIds.has(entry.id));
  const selectedNeedsWork = selectedEntries.filter((entry) => contextEntryPresentation(entry).needsWork).length;
  document.getElementById("context-entry-count").textContent = data.counts.included.toLocaleString();
  document.getElementById("context-pinned-count").textContent = data.counts.pinned.toLocaleString();
  document.getElementById("context-needs-work-count").textContent = selectedNeedsWork.toLocaleString();
  document.getElementById("context-token-count").textContent = `~${data.token_estimate.toLocaleString()}`;
  document.getElementById("context-corpus-count").textContent =
    `${data.counts.total.toLocaleString()} audit records · ~${data.source_tokens.toLocaleString()} corpus tokens retained`;

  const limit = document.getElementById("context-limit");
  const configured = String(data.settings.recent_limit);
  if (![...limit.options].some((option) => option.value === configured)) {
    limit.appendChild(new Option(configured, configured));
  }
  limit.value = configured;

  const entriesPanel = document.getElementById("context-entries");
  const mapPanel = document.getElementById("context-map");
  const previewPanel = document.getElementById("context-preview-panel");
  const preview = document.getElementById("context-preview");
  const showEntries = state.contextTab === "entries";
  const showMap = state.contextTab === "map";
  entriesPanel.hidden = !showEntries;
  mapPanel.hidden = !showMap;
  previewPanel.hidden = state.contextTab !== "preview";
  document.getElementById("context-filters").hidden = !showEntries;
  document.getElementById("context-tab-entries").classList.toggle("active", showEntries);
  document.getElementById("context-tab-map").classList.toggle("active", showMap);
  document.getElementById("context-tab-preview").classList.toggle("active", state.contextTab === "preview");
  document.getElementById("context-tab-entries").setAttribute("aria-selected", String(showEntries));
  document.getElementById("context-tab-map").setAttribute("aria-selected", String(showMap));
  document.getElementById("context-tab-preview").setAttribute("aria-selected", String(state.contextTab === "preview"));
  preview.textContent = data.preview || "No context is currently selected for delivery.";

  entriesPanel.replaceChildren();
  const filtered = data.entries.filter((entry) => contextEntryMatches(entry, selectedIds));
  const board = el("div", "context-category-board");
  for (const category of data.categories) {
    appendContextCategory(
      board,
      category,
      filtered.filter((entry) => entry.category === category),
      selectedIds
    );
  }
  entriesPanel.appendChild(board);
  renderContextMap();
}

async function saveContextNote() {
  const textarea = document.getElementById("context-note-content");
  const content = textarea.value.trim();
  if (!content) {
    setContextStatus("Briefing text cannot be empty.", true);
    textarea.focus();
    return;
  }
  const saved = await mutateContext(
    "/api/context/notes",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_path: state.currentProject,
        content,
        category: document.getElementById("context-note-category").value,
      }),
    },
    "Failed to add project briefing."
  );
  if (saved) {
    textarea.value = "";
    document.getElementById("context-note-form").hidden = true;
  }
}

async function sendRecategorize() {
  if (!state.currentProject) return;
  const textarea = document.getElementById("context-recategorize-instructions");
  const instructions = textarea.value.trim();
  if (!instructions) {
    setContextStatus("Instructions cannot be empty.", true);
    textarea.focus();
    return;
  }
  const agent = document.getElementById("context-recategorize-agent").value;
  const btn = document.getElementById("context-recategorize-send");
  btn.disabled = true;
  try {
    const res = await apiFetch("/api/context/recategorize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_path: state.currentProject,
        agent,
        instructions,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to start recategorization.");
    textarea.value = "";
    document.getElementById("context-recategorize-form").hidden = true;
    runningDispatchJobs.add(data.job_id);
    state.recategorizeJobs.add(data.job_id);
    state.expandedJobs.add(data.job_id);
    await loadDispatchHistory();
    await pollActiveAgents();
    showToast(`${agent} is recategorizing the briefing…`, false);
  } catch (err) {
    setContextStatus(err.message, true);
  } finally {
    btn.disabled = false;
  }
}


const runningDispatchJobs = new Set();

function formatElapsed(createdAt) {
  const started = Date.parse(createdAt || "");
  if (!Number.isFinite(started)) return "just started";
  const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function refreshAgentElapsedTimes() {
  document.querySelectorAll(".agent-elapsed[data-created-at]").forEach((node) => {
    const elapsed = formatElapsed(node.dataset.createdAt);
    if (node.dataset.status === "waiting") {
      node.textContent = `Waiting for input · run age ${elapsed}`;
    } else if (node.dataset.status === "canceling") {
      node.textContent = `Canceling · elapsed ${elapsed}`;
    } else {
      node.textContent = `Elapsed ${elapsed} · completion time unavailable`;
    }
  });
}

function renderAgentProgress(job, compact = false) {
  const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
  const box = el("div", `agent-progress ${job.status}${compact ? " compact" : ""}`);
  const status = el("div", "agent-progress-status");
  status.appendChild(el("span", "agent-progress-label", job.progress_label || "Agent is working"));
  const activity = Number(job.activity_count) || 0;
  status.appendChild(el("span", "agent-progress-activity", activity ? `${activity} updates` : "Starting"));
  box.appendChild(status);

  const track = el("div", "agent-progress-track");
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", "100");
  track.setAttribute("aria-valuenow", String(progress));
  track.setAttribute("aria-label", "Observed agent activity. This is not an estimated percent complete.");
  track.title = "Activity indicator. The agent CLI does not report a completion estimate.";
  const fill = el("span", "agent-progress-fill");
  fill.style.width = `${progress}%`;
  track.appendChild(fill);
  box.appendChild(track);

  const timing = el("div", "agent-elapsed");
  timing.dataset.createdAt = job.created_at || "";
  timing.dataset.status = job.status;
  box.appendChild(timing);
  return box;
}

async function loadInteractions() {
  const res = await apiFetch("/api/interactions?pending_only=true");
  const interactions = await res.json();
  if (!res.ok) throw new Error(interactions.detail || "Failed to load agent questions.");
  const signature = JSON.stringify(interactions.map((item) => item.id));
  for (const interaction of interactions) {
    if (!state.knownInteractionIds.has(interaction.id)) {
      state.knownInteractionIds.add(interaction.id);
      showToast(`${interaction.agent} needs input: ${interaction.prompt.slice(0, 120)}`, false);
    }
  }
  state.pendingInteractions = interactions;
  if (signature !== state.interactionsSignature) {
    renderInteractions(interactions);
    state.interactionsSignature = signature;
  }
}

function renderInteractions(interactions) {
  const panel = document.getElementById("interaction-panel");
  const list = document.getElementById("interaction-list");
  panel.style.display = interactions.length ? "" : "none";
  document.getElementById("interaction-count").textContent = interactions.length || "";
  document.title = interactions.length ? `(${interactions.length}) AgenticSync` : "AgenticSync";
  list.innerHTML = "";
  for (const interaction of interactions) {
    const card = el("article", "interaction-card");
    const heading = el("div", "interaction-card-heading");
    heading.appendChild(el("span", "agent-tag " + agentClass(interaction.agent), interaction.agent));
    heading.appendChild(el("span", "interaction-kind", interaction.kind));
    heading.appendChild(el("span", "interaction-project", shortPath(interaction.project_path)));
    card.appendChild(heading);
    card.appendChild(el("div", "interaction-prompt", interaction.prompt));

    if (interaction.options.length) {
      const choices = el("div", "interaction-options");
      for (const option of interaction.options) {
        const choice = el("button", "btn btn-secondary btn-tiny", option);
        choice.type = "button";
        choice.addEventListener("click", () => respondToInteraction(interaction, option, card));
        choices.appendChild(choice);
      }
      card.appendChild(choices);
    }

    const form = el("div", "interaction-response");
    const input = document.createElement("textarea");
    input.placeholder = "Type a response for the agent";
    input.maxLength = 4000;
    input.setAttribute("aria-label", "Response to agent");
    const send = el("button", "btn", "Send response");
    send.type = "button";
    send.addEventListener("click", () => respondToInteraction(interaction, input.value, card));
    input.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        respondToInteraction(interaction, input.value, card);
      }
    });
    form.appendChild(input);
    form.appendChild(send);
    card.appendChild(form);
    card.appendChild(el("div", "interaction-status status"));
    list.appendChild(card);
  }
}

async function respondToInteraction(interaction, rawResponse, card) {
  const response = String(rawResponse || "").trim();
  const status = card.querySelector(".interaction-status");
  if (!response) {
    status.className = "interaction-status status error";
    status.textContent = "Enter a response first.";
    return;
  }
  card.querySelectorAll("button, textarea").forEach((control) => { control.disabled = true; });
  status.className = "interaction-status status";
  status.textContent = "Sending response and resuming the agent...";
  try {
    const res = await apiFetch(`/api/interactions/${interaction.id}/respond`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ response }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to respond.");
    state.interactionsSignature = "";
    showToast("Response sent. The deployment is resuming.", false);
    await loadInteractions();
    if (state.currentProject === interaction.project_path) await loadDispatchHistory(true);
    await pollActiveAgents();
  } catch (err) {
    status.className = "interaction-status status error";
    status.textContent = err.message;
    card.querySelectorAll("button, textarea").forEach((control) => { control.disabled = false; });
  }
}

function parseLogEntry(raw) {
  try {
    const e = JSON.parse(raw);
    if (e && typeof e === "object" && "t" in e) return { k: e.k || "info", t: e.t };
  } catch {}
  return { k: "info", t: raw };
}

function renderLogEntry(entry) {
  const div = el("div", "logline " + (entry.k || "info"));
  div.textContent = entry.t;
  return div;
}

async function sendDispatch() {
  if (!state.currentProject) return;
  const promptEl = document.getElementById("dispatch-prompt");
  let prompt = promptEl.value.trim();
  if (!prompt) return;
  if (state.attachedFiles.length) {
    prompt +=
      "\n\nAttached files (already saved in this repo, read them as needed): " +
      state.attachedFiles.map((f) => f.path).join(", ");
  }
  const agent = document.getElementById("dispatch-agent").value;
  const allowEdits = document.getElementById("dispatch-allow-edits").checked;
  const btn = document.getElementById("dispatch-send");
  btn.disabled = true;
  try {
    const res = await apiFetch("/api/dispatch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_path: state.currentProject,
        agent,
        prompt,
        allow_edits: allowEdits,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to deploy.");
    promptEl.value = "";
    state.attachedFiles = [];
    renderAttachedFiles();
    runningDispatchJobs.add(data.job_id);
    state.expandedJobs.add(data.job_id);
    await loadDispatchHistory();
    await pollActiveAgents();
  } catch (err) {
    showToast("Deploy failed: " + err.message, true);
  } finally {
    btn.disabled = false;
  }
}

function dispatchJobsSignature(jobs) {
  return JSON.stringify(jobs.map((job) => [
    job.id, job.status, job.tokens, job.finished_at,
    (job.result_text || "").length, job.context_tokens,
    job.progress, job.progress_label, job.activity_count,
    job.parent_job_id, job.coordination_id, job.task_label, job.model,
  ]));
}

async function loadDispatchHistory(forceRender = false) {
  if (!state.currentProject) return;
  const project = state.currentProject;
  const version = ++state.dispatchRequestVersion;
  const res = await apiFetch("/api/dispatch?project=" + encodeURIComponent(project));
  const jobs = await res.json();
  if (project !== state.currentProject || version !== state.dispatchRequestVersion) return;
  const signature = dispatchJobsSignature(jobs);
  state.dispatchJobs = jobs;
  if (forceRender || signature !== state.dispatchJobsSignature) {
    renderDispatchHistory(jobs);
    state.dispatchJobsSignature = signature;
  }
  document.getElementById("runs-count").textContent = jobs.length ? String(jobs.length) : "";
  for (const job of jobs) {
    if (job.status === "running") runningDispatchJobs.add(job.id);
    else runningDispatchJobs.delete(job.id);
    if (state.expandedJobs.has(job.id)) fetchJobLogs(job.id);
    if (state.recategorizeJobs.has(job.id) && job.status !== "running") {
      state.recategorizeJobs.delete(job.id);
      if (job.status === "done") {
        showToast("Categories updated by " + job.agent + ".", false);
        loadContext();
      } else if (job.status === "error") {
        showToast("Recategorization failed: " + (job.result_text || "unknown error"), true);
      }
    }
  }
}

async function fetchJobLogs(jobId) {
  const since = state.logCursors[jobId] || 0;
  const res = await apiFetch(`/api/dispatch/${jobId}/logs?since_id=${since}`);
  const logs = await res.json();
  if (!logs.length) return;
  const parsed = logs.map((l) => parseLogEntry(l.line));
  state.logsByJob[jobId] = (state.logsByJob[jobId] || []).concat(parsed);
  state.logCursors[jobId] = logs[logs.length - 1].id;
  const box = document.getElementById("log-" + jobId);
  if (box) {
    const scrollState = state.logScrollByJob[jobId];
    const shouldFollow = !scrollState || scrollState.atBottom;
    for (const entry of parsed) box.appendChild(renderLogEntry(entry));
    if (shouldFollow) box.scrollTop = box.scrollHeight;
    rememberLogScroll(box);
  }
}

function rememberLogScroll(box) {
  const jobId = box.dataset.jobId;
  if (!jobId) return;
  const bottomGap = box.scrollHeight - box.clientHeight - box.scrollTop;
  state.logScrollByJob[jobId] = {
    scrollTop: box.scrollTop,
    atBottom: bottomGap <= 8,
  };
}

function toggleJob(jobId) {
  if (state.expandedJobs.has(jobId)) state.expandedJobs.delete(jobId);
  else {
    state.expandedJobs.add(jobId);
    state.logCursors[jobId] = 0;
    state.logsByJob[jobId] = [];
    delete state.logScrollByJob[jobId];
  }
  renderDispatchHistory(state.dispatchJobs);
  if (state.expandedJobs.has(jobId)) fetchJobLogs(jobId);
}

async function loadContextSnapshot(details, jobId) {
  if (!details.open || details.querySelector("pre")) return;
  const loading = el("div", "context-snapshot-loading", "Loading context snapshot…");
  details.appendChild(loading);
  try {
    let snapshot = state.contextSnapshotCache[jobId];
    if (snapshot === undefined) {
      const res = await apiFetch(`/api/dispatch/${jobId}`);
      const job = await res.json();
      if (!res.ok) throw new Error(job.detail || "Failed to load context snapshot.");
      snapshot = job.context_snapshot || "No pooled context was available when this job was queued.";
      state.contextSnapshotCache[jobId] = snapshot;
    }
    if (!details.open) return;
    details.appendChild(el("pre", "context-preview dispatch-snapshot-preview", snapshot));
  } catch (err) {
    if (details.open) details.appendChild(el("div", "status error", err.message));
  } finally {
    loading.remove();
  }
}

function renderDispatchHistory(jobs) {
  const container = document.getElementById("dispatch-history-list");
  const listScrollTop = container.scrollTop;
  const owner = container.closest(".feed, .aside");
  const ownerScrollTop = owner?.scrollTop || 0;
  for (const details of container.querySelectorAll(".dispatch-context-snapshot[open]")) {
    if (details.dataset.jobId) state.openContextSnapshots.add(details.dataset.jobId);
  }
  for (const box of container.querySelectorAll(".agent-log")) {
    rememberLogScroll(box);
  }
  if (jobs.length === 0) {
    container.innerHTML = '<p class="empty">No agents deployed yet.</p>';
    return;
  }
  container.innerHTML = "";
  for (const job of jobs) {
    const row = el("div", "dispatch-row");
    if (job.parent_job_id) row.classList.add("coordinated-child");
    const header = el("div", "prompt");
    header.appendChild(
      el("span", "agent-tag " + agentClass(job.agent), job.agent)
    );
    if (job.task_label) {
      header.appendChild(el("span", "coordination-task-label", job.task_label));
    } else {
      header.appendChild(document.createTextNode(" " + job.prompt));
    }
    const st = el("span", "dispatch-status " + job.status, job.status);
    if (job.status === "running") st.classList.add("pulse");
    header.appendChild(st);
    header.addEventListener("click", () => toggleJob(job.id));
    row.appendChild(header);

    const isLive = job.status === "running" || job.status === "waiting" || job.status === "canceling";
    if (isLive) row.appendChild(renderAgentProgress(job));

    const expanded = state.expandedJobs.has(job.id);
    if (expanded) {
      const logBox = el("div", "agent-log");
      logBox.id = "log-" + job.id;
      logBox.dataset.jobId = job.id;
      logBox.addEventListener("scroll", () => rememberLogScroll(logBox));
      for (const entry of state.logsByJob[job.id] || []) {
        logBox.appendChild(renderLogEntry(entry));
      }
      row.appendChild(logBox);
    }

    if (job.result_text && job.status !== "waiting" && (expanded || job.status !== "running")) {
      row.appendChild(el("div", "result", job.result_text));
    }
    const tokenStr = job.tokens ? ` · ${job.tokens.toLocaleString()} tokens` : "";
    const meta = el(
      "div",
      "meta",
      `${job.allow_edits ? "edits allowed" : "read-only"}` +
        `${job.model ? ` · ${job.model}` : ""}${tokenStr} · ${job.created_at}` +
        (expanded ? "" : "  (click to expand logs)")
    );
    row.appendChild(meta);

    if (job.parent_job_id) {
      row.appendChild(el("div", "coordination-origin", `Coordinated follow-up · source ${job.parent_job_id.slice(0, 8)}`));
    } else {
      const children = jobs.filter((candidate) => candidate.parent_job_id === job.id);
      if (children.length) {
        const complete = children.filter((candidate) => ["done", "error", "canceled"].includes(candidate.status)).length;
        row.appendChild(el("div", "coordination-summary", `${complete}/${children.length} coordinated tasks settled`));
      }
    }

    const snapshot = el("details", "dispatch-context-snapshot");
    snapshot.dataset.jobId = job.id;
    snapshot.open = state.openContextSnapshots.has(job.id);
    const contextTokens = job.context_tokens ? ` · ${job.context_tokens.toLocaleString()} tokens` : "";
    const snapshotSummary = el("summary", "", "Context used" + contextTokens);
    snapshotSummary.title =
      "Size of the pooled-context snapshot prefixed to this run, not a cap. " +
      "the agent can search the full raw corpus for more.";
    snapshot.appendChild(snapshotSummary);
    snapshot.addEventListener("toggle", () => {
      if (snapshot.open) {
        state.openContextSnapshots.add(job.id);
        loadContextSnapshot(snapshot, job.id);
      } else {
        state.openContextSnapshots.delete(job.id);
      }
    });
    row.appendChild(snapshot);

    const rowActions = el("div", "dispatch-row-actions");
    if (isLive) {
      const cancelBtn = el("button", "btn btn-secondary btn-tiny", "Terminate");
      cancelBtn.title = "Stop this agent immediately.";
      cancelBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        cancelDispatchJob(job.id);
      });
      rowActions.appendChild(cancelBtn);
    }
    const redirectBtn = el("button", "btn btn-secondary btn-tiny", "Redirect");
    redirectBtn.title = isLive
      ? "Stop this agent and continue its session in a new direction."
      : "Start a follow-up run continuing this session in a new direction.";
    redirectBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      startRedirectJob(job.id);
    });
    rowActions.appendChild(redirectBtn);

    if (!isLive) {
      const coordinateBtn = el("button", "btn btn-secondary btn-tiny", "Coordinate follow-up");
      coordinateBtn.title = "Split deferred work from this deployment across multiple agents or models.";
      coordinateBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        openCoordinationBuilder(job);
      });
      rowActions.appendChild(coordinateBtn);
    }

    if (job.status === "done") {
      const hb = el("button", "btn btn-secondary btn-tiny", "→ Handoff to Claude Code");
      hb.addEventListener("click", (e) => {
        e.stopPropagation();
        createHandoff(job.id);
      });
      rowActions.appendChild(hb);
    }
    const deleteBtn = el("button", "btn btn-secondary btn-tiny btn-danger", "Delete");
    deleteBtn.title = "Remove this run from history.";
    deleteBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteDispatchJob(job.id);
    });
    rowActions.appendChild(deleteBtn);
    row.appendChild(rowActions);
    container.appendChild(row);
    if (snapshot.open) loadContextSnapshot(snapshot, job.id);

    if (expanded) {
      const logBox = document.getElementById("log-" + job.id);
      const scrollState = state.logScrollByJob[job.id];
      if (scrollState?.atBottom) logBox.scrollTop = logBox.scrollHeight;
      else if (scrollState) logBox.scrollTop = scrollState.scrollTop;
      else logBox.scrollTop = logBox.scrollHeight;
      rememberLogScroll(logBox);
    }
  }
  container.scrollTop = listScrollTop;
  if (owner) owner.scrollTop = ownerScrollTop;
  refreshAgentElapsedTimes();
}

function createCoordinationTaskRow(index, defaults = {}) {
  const row = el("div", "coordination-task-row");
  const heading = el("div", "coordination-task-heading");
  heading.appendChild(el("strong", "", `Task ${index + 1}`));
  const remove = el("button", "btn btn-secondary btn-tiny", "Remove");
  remove.type = "button";
  remove.addEventListener("click", () => {
    const list = row.parentElement;
    if (list.children.length <= 2) {
      showToast("A coordination batch needs at least two tasks.", true);
      return;
    }
    row.remove();
    [...list.children].forEach((child, i) => {
      child.querySelector("strong").textContent = `Task ${i + 1}`;
    });
  });
  heading.appendChild(remove);
  row.appendChild(heading);

  const label = document.createElement("input");
  label.className = "coordination-label";
  label.placeholder = "Feature or workstream name";
  label.maxLength = 120;
  label.value = defaults.label || "";
  row.appendChild(label);

  const prompt = document.createElement("textarea");
  prompt.className = "coordination-prompt";
  prompt.placeholder = "A bounded assignment for this worker";
  prompt.maxLength = 12000;
  prompt.value = defaults.prompt || "";
  row.appendChild(prompt);

  const controls = el("div", "coordination-task-controls");
  const agent = document.createElement("select");
  agent.className = "coordination-agent";
  const rotation = dispatchableAgents();
  const defaultAgent = defaults.agent || (rotation[index % rotation.length] || rotation[0])?.id || "claude-code";
  agent.dataset.defaultAgent = defaultAgent;
  populateAgentSelect(agent, defaultAgent);
  controls.appendChild(agent);

  const model = document.createElement("input");
  model.className = "coordination-model";
  model.placeholder = "Model (agent default)";
  model.maxLength = 200;
  model.value = defaults.model || "";
  controls.appendChild(model);

  const edits = el("label", "dispatch-checkbox");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = Boolean(defaults.allow_edits);
  edits.appendChild(checkbox);
  edits.appendChild(document.createTextNode(" Allow edits"));
  controls.appendChild(edits);
  row.appendChild(controls);
  return row;
}

function openCoordinationBuilder(sourceJob) {
  document.getElementById("coordination-dialog")?.remove();
  const dialog = document.createElement("dialog");
  dialog.id = "coordination-dialog";
  dialog.className = "coordination-dialog";

  const form = document.createElement("form");
  form.method = "dialog";
  const head = el("div", "coordination-dialog-head");
  const titleBlock = el("div", "");
  titleBlock.appendChild(el("div", "context-eyebrow", "Selected deployment"));
  titleBlock.appendChild(el("h2", "", "Coordinate follow-up work"));
  titleBlock.appendChild(el("p", "", compactText(sourceJob.prompt, 180)));
  head.appendChild(titleBlock);
  const close = el("button", "btn btn-secondary btn-tiny", "Close");
  close.type = "button";
  close.addEventListener("click", () => dialog.close());
  head.appendChild(close);
  form.appendChild(head);

  form.appendChild(el("p", "coordination-help", "Create 2 to 8 bounded tasks. They start in parallel with this deployment's saved context. Use edit access only where tasks do not overlap."));
  const list = el("div", "coordination-task-list");
  list.appendChild(createCoordinationTaskRow(0));
  list.appendChild(createCoordinationTaskRow(1));
  form.appendChild(list);

  const actions = el("div", "coordination-dialog-actions");
  const add = el("button", "btn btn-secondary", "Add task");
  add.type = "button";
  add.addEventListener("click", () => {
    if (list.children.length >= 8) {
      showToast("A coordination batch supports up to eight tasks.", true);
      return;
    }
    list.appendChild(createCoordinationTaskRow(list.children.length));
  });
  actions.appendChild(add);
  const launch = el("button", "btn", "Launch coordinated agents");
  launch.type = "button";
  launch.addEventListener("click", () => launchCoordination(sourceJob.id, dialog, list, launch));
  actions.appendChild(launch);
  form.appendChild(actions);
  dialog.appendChild(form);
  dialog.addEventListener("close", () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
}

async function launchCoordination(sourceJobId, dialog, list, launchButton) {
  const tasks = [...list.children].map((row) => ({
    label: row.querySelector(".coordination-label").value.trim(),
    prompt: row.querySelector(".coordination-prompt").value.trim(),
    agent: row.querySelector("select").value,
    model: row.querySelector(".coordination-model").value.trim(),
    allow_edits: row.querySelector('input[type="checkbox"]').checked,
  }));
  if (tasks.some((task) => !task.label || !task.prompt)) {
    showToast("Every coordinated task needs a name and assignment.", true);
    return;
  }
  launchButton.disabled = true;
  try {
    const res = await apiFetch(`/api/dispatch/${sourceJobId}/coordinate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tasks }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Coordination failed.");
    for (const jobId of data.job_ids) runningDispatchJobs.add(jobId);
    dialog.close();
    await loadDispatchHistory(true);
    await pollActiveAgents();
    showToast(`${data.job_ids.length} coordinated agents launched.`, false);
  } catch (err) {
    showToast("Coordination failed: " + err.message, true);
    launchButton.disabled = false;
  }
}

async function createHandoff(jobId) {
  try {
    const res = await apiFetch(`/api/dispatch/${jobId}/handoff`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Handoff failed.");
    document.getElementById("dispatch-agent").value = "claude-code";
    document.getElementById("dispatch-allow-edits").checked = true;
    document.getElementById("dispatch-prompt").value =
      `Read the handoff at ${data.path}, verify its plan against the actual ` +
      `codebase and tests, then implement it. Don't take the handoff on faith. ` +
      `confirm against real files and flag anything that doesn't match.`;
    document.getElementById("dispatch-panel").scrollIntoView({ behavior: "smooth" });
    showToast("Handoff written to " + data.path + ". Review the prefilled prompt, then Deploy.", false);
  } catch (err) {
    showToast("Handoff failed: " + err.message, true);
  }
}

async function cancelDispatchJob(jobId) {
  try {
    const res = await apiFetch(`/api/dispatch/${jobId}/cancel`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Terminate failed.");
    runningDispatchJobs.delete(jobId);
    await loadDispatchHistory(true);
    await pollActiveAgents();
  } catch (err) {
    showToast("Terminate failed: " + err.message, true);
  }
}

async function deleteDispatchJob(jobId) {
  if (!window.confirm("Delete this agent run? This cannot be undone.")) return;
  try {
    const res = await apiFetch(`/api/dispatch/${jobId}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Delete failed.");
    state.expandedJobs.delete(jobId);
    runningDispatchJobs.delete(jobId);
    delete state.logsByJob[jobId];
    delete state.logCursors[jobId];
    delete state.logScrollByJob[jobId];
    delete state.contextSnapshotCache[jobId];
    await loadDispatchHistory(true);
    await pollActiveAgents();
  } catch (err) {
    showToast("Delete failed: " + err.message, true);
  }
}

function startRedirectJob(jobId) {
  const direction = window.prompt("Redirect this agent. Describe the new direction to take:");
  if (!direction || !direction.trim()) return;
  redirectDispatchJob(jobId, direction.trim());
}

async function redirectDispatchJob(jobId, direction) {
  try {
    const res = await apiFetch(`/api/dispatch/${jobId}/redirect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direction }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Redirect failed.");
    runningDispatchJobs.delete(jobId);
    runningDispatchJobs.add(data.job_id);
    state.expandedJobs.add(data.job_id);
    await loadDispatchHistory(true);
    await pollActiveAgents();
    showToast("Redirected. New run queued in the updated direction.", false);
  } catch (err) {
    showToast("Redirect failed: " + err.message, true);
  }
}


const PRESETS = {
  research: {
    agent: "codex",
    allowEdits: false,
    text: "Research this codebase and the task below, then produce a concrete plan. Explore relevant files and summarize findings. Do not edit anything.\n\nTask: ",
  },
  plan: {
    agent: "claude-code",
    allowEdits: false,
    text: "Draft a step-by-step implementation plan for the task below. Read the relevant files, list the exact changes per file, and call out risks. Do not make edits.\n\nTask: ",
  },
  execute: {
    agent: "claude-code",
    allowEdits: true,
    text: "Implement the following. Make the edits, then briefly summarize what changed.\n\nTask: ",
  },
  review: {
    agent: "codex",
    allowEdits: false,
    text: "Review the current state of this repo (recent changes, correctness, risks) and report findings. Read-only.\n\nFocus: ",
  },
};

function applyPreset(name) {
  const p = PRESETS[name];
  if (!p) return;
  document.getElementById("dispatch-agent").value = p.agent;
  document.getElementById("dispatch-allow-edits").checked = p.allowEdits;
  const ta = document.getElementById("dispatch-prompt");
  ta.value = p.text;
  ta.focus();
  ta.selectionStart = ta.selectionEnd = ta.value.length;
}


async function saveAgentModel(agent, input) {
  const model = input.value.trim();
  input.disabled = true;
  try {
    const res = await apiFetch("/api/agents/models", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent, model }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to save model.");
    input.value = data.model || "";
    input.title = data.model ? `Dispatches use ${data.model}` : "Uses the CLI's own default model";
  } catch (err) {
    showToast("Model not saved: " + err.message, true);
  } finally {
    input.disabled = false;
  }
}

function populateAgentSelect(select, defaultId) {
  const current = select.value;
  select.innerHTML = "";
  for (const agent of dispatchableAgents()) {
    const option = document.createElement("option");
    option.value = agent.id;
    option.textContent = agent.display_name;
    select.appendChild(option);
  }
  const fallback = dispatchableAgents()[0]?.id || "claude-code";
  select.value = [current, defaultId, fallback].find(
    (v) => v && select.querySelector(`option[value="${CSS.escape(v)}"]`)
  ) || fallback;
}

function populateAllAgentSelects() {
  const dispatchSelect = document.getElementById("dispatch-agent");
  if (dispatchSelect) populateAgentSelect(dispatchSelect, "claude-code");
  document.querySelectorAll("select.coordination-agent").forEach((select) => {
    populateAgentSelect(select, select.dataset.defaultAgent);
  });
}

async function loadAgentStatus() {
  await loadAgentsCache();
  populateAllAgentSelects();

  let data;
  try {
    data = await (await apiFetch("/api/agents/status")).json();
  } catch {
    data = {};
  }
  let models = {};
  try {
    models = await (await apiFetch("/api/agents/models")).json();
  } catch {}
  const box = document.getElementById("agent-conn");
  if (box) {
    box.innerHTML = "";
    for (const agent of dispatchableAgents()) {
      const s = data[agent.id] || {};
      const dot = s.found ? "🟢" : "🔴";
      const label = agent.display_name;
      const chip = el("span", "conn-chip", `${dot} ${label} ${s.found ? "connected" : "not found"}`);
      if (!s.found && s.detail) chip.title = s.detail;
      box.appendChild(chip);

      const modelInput = document.createElement("input");
      modelInput.type = "text";
      modelInput.className = "conn-model-input";
      modelInput.placeholder = agent.capabilities?.history === false ? "Model" : "CLI default model";
      modelInput.value = models[agent.id] || "";
      modelInput.title = models[agent.id] ? `Dispatches use ${models[agent.id]}` : "Uses the provider's own default model";
      modelInput.setAttribute("aria-label", `Model override for ${label}`);
      modelInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") modelInput.blur();
      });
      modelInput.addEventListener("blur", () => saveAgentModel(agent.id, modelInput));
      box.appendChild(modelInput);
    }
    const hint = el("span", "conn-hint", "");
    hint.innerHTML =
      'If a dispatch to Claude Code says "not logged in", run <code>claude</code> once in a terminal and sign in. ' +
      "Leave a model field blank to use that CLI's/provider's own default.";
    box.appendChild(hint);
  }
  updateDispatchConnSummary(data);
  await loadLocalProviders();
}

async function loadLocalProviders() {
  const box = document.getElementById("local-providers");
  if (!box) return;
  let providers = [];
  try {
    providers = await (await apiFetch("/api/agents/providers")).json();
  } catch {
    return;
  }
  box.innerHTML = "";
  box.appendChild(el("h3", "local-providers-title", "Local / self-hosted models"));
  const list = el("div", "local-providers-list");
  if (!providers.length) {
    list.appendChild(el("div", "local-providers-empty", "No local providers registered yet."));
  }
  for (const provider of providers) {
    const row = el("div", "local-provider-row");
    row.appendChild(el("span", "local-provider-name", provider.display_name));
    row.appendChild(el("span", "local-provider-detail", `${provider.base_url} · ${provider.model}`));
    const testBtn = el("button", "btn-secondary btn-tiny", "Test");
    testBtn.type = "button";
    testBtn.addEventListener("click", async () => {
      testBtn.textContent = "Testing…";
      testBtn.disabled = true;
      try {
        const res = await apiFetch(`/api/agents/providers/${encodeURIComponent(provider.agent_id)}/health`, {
          method: "POST",
        });
        const result = await res.json();
        showToast(
          result.reachable ? `${provider.display_name} is reachable.` : `${provider.display_name}: ${result.detail || "unreachable"}`,
          !result.reachable
        );
      } catch (err) {
        showToast("Health check failed: " + err.message, true);
      } finally {
        testBtn.textContent = "Test";
        testBtn.disabled = false;
      }
    });
    row.appendChild(testBtn);
    const deleteBtn = el("button", "btn-danger btn-tiny", "Remove");
    deleteBtn.type = "button";
    deleteBtn.addEventListener("click", async () => {
      if (!confirm(`Remove ${provider.display_name}?`)) return;
      try {
        await apiFetch(`/api/agents/providers/${encodeURIComponent(provider.agent_id)}`, { method: "DELETE" });
        await loadAgentStatus();
      } catch (err) {
        showToast("Failed to remove provider: " + err.message, true);
      }
    });
    row.appendChild(deleteBtn);
    list.appendChild(row);
  }
  box.appendChild(list);

  const form = el("form", "local-provider-form");
  const fields = [
    ["agent_id", "id (e.g. ollama-gemma3)"],
    ["display_name", "Display name"],
    ["base_url", "Base URL (e.g. http://localhost:11434)"],
    ["model", "Model (e.g. gemma3:4b)"],
    ["api_key_env", "API key env var (optional)"],
  ];
  const inputs = {};
  for (const [name, placeholder] of fields) {
    const input = document.createElement("input");
    input.type = "text";
    input.name = name;
    input.placeholder = placeholder;
    input.className = "local-provider-input";
    inputs[name] = input;
    form.appendChild(input);
  }
  const submit = el("button", "btn", "Add local model");
  submit.type = "submit";
  form.appendChild(submit);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    submit.disabled = true;
    try {
      const res = await apiFetch("/api/agents/providers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: inputs.agent_id.value.trim(),
          display_name: inputs.display_name.value.trim(),
          base_url: inputs.base_url.value.trim(),
          model: inputs.model.value.trim(),
          api_key_env: inputs.api_key_env.value.trim(),
        }),
      });
      const result = await res.json();
      if (!res.ok) throw new Error(result.detail || "Failed to save provider.");
      showToast(`${result.display_name} added.`, false);
      await loadAgentStatus();
    } catch (err) {
      showToast("Not saved: " + err.message, true);
    } finally {
      submit.disabled = false;
    }
  });
  box.appendChild(form);
}

function updateDispatchConnSummary(status) {
  const el2 = document.getElementById("dispatch-conn-summary");
  if (!el2) return;
  const parts = dispatchableAgents().map((agent) => {
    const found = (status[agent.id] || {}).found;
    return `${found ? "🟢" : "🔴"} ${agent.display_name}`;
  });
  el2.textContent = parts.join("   ");
}

// ---------- Settings (agent connections & models) ----------

function openSettings() {
  const modal = document.getElementById("settings-modal");
  if (!modal) return;
  modal.hidden = false;
  loadAgentStatus(); // refresh chips, models, and local providers on open
}

function closeSettings() {
  const modal = document.getElementById("settings-modal");
  if (modal) modal.hidden = true;
}

async function pollActiveAgents() {
  let jobs = [];
  try {
    const res = await apiFetch("/api/agents/active");
    jobs = await res.json();
  } catch {
    return;
  }
  const bar = document.getElementById("active-agents-bar");
  const strip = document.getElementById("active-agents-strip");
  if (!jobs.length) {
    bar.style.display = "none";
    strip.innerHTML = "";
    return;
  }
  bar.style.display = "flex";
  strip.innerHTML = "";
  for (const job of jobs) {
    const chip = el("div", "active-agent-chip");
    chip.appendChild(el("div", "who " + agentClass(job.agent), job.agent + " · " + shortPath(job.project_path)));
    chip.appendChild(el("div", "latest", job.prompt));
    chip.appendChild(renderAgentProgress(job, true));
    chip.addEventListener("click", () => {
      selectProject(job.project_path);
      state.expandedJobs.add(job.id);
      loadDispatchHistory();
    });
    const stopBtn = el("button", "chip-stop", "✕");
    stopBtn.type = "button";
    stopBtn.title = "Terminate this agent.";
    stopBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      cancelDispatchJob(job.id);
    });
    chip.appendChild(stopBtn);
    strip.appendChild(chip);
  }
  refreshAgentElapsedTimes();
}


function renderAttachedFiles() {
  const box = document.getElementById("attached-files");
  if (!box) return;
  box.innerHTML = "";
  for (const f of state.attachedFiles) {
    box.appendChild(el("span", "attached-file", "📎 " + f.name));
  }
}

async function uploadFile(file) {
  if (!state.currentProject || !file) return;
  const form = new FormData();
  form.append("project", state.currentProject);
  form.append("file", file);
  try {
    const res = await apiFetch("/api/upload", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Upload failed.");
    state.attachedFiles.push(data);
    renderAttachedFiles();
    showToast("Uploaded " + data.name + " into the repo", false);
  } catch (err) {
    showToast("Upload failed: " + err.message, true);
  }
}


async function syncNativeHistory(project, force = false) {
  if (!project || (!force && state.syncedProjects.has(project))) return null;
  state.syncedProjects.add(project);
  try {
    const res = await apiFetch("/api/projects/backfill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: project }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Native history sync failed.");
    return data;
  } catch (err) {
    state.syncedProjects.delete(project);
    throw err;
  }
}

async function backfillHistory() {
  if (!state.currentProject) return;
  const btn = document.getElementById("backfill-btn");
  const status = document.getElementById("backfill-status");
  btn.disabled = true;
  status.className = "status";
  status.textContent = "Importing existing Claude + Codex history...";
  try {
    const data = await syncNativeHistory(state.currentProject, true);
    status.textContent =
      `Synced ${data.imported_claude} new Claude + ${data.imported_codex} new Codex sessions; ` +
      `${data.refreshed} existing native transcripts refreshed.`;
    state.lastMaxEventId = 0;
    await pollEvents();
    await loadTelemetry();
    await loadContext();
  } catch (err) {
    status.className = "status error";
    status.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}


async function addProject() {
  const input = document.getElementById("add-project-path");
  const status = document.getElementById("add-project-status");
  const path = input.value.trim();
  if (!path) return;
  const btn = document.getElementById("add-project-btn");
  btn.disabled = true;
  status.className = "status";
  status.textContent = "Adding...";
  try {
    const res = await apiFetch("/api/projects/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to add project.");
    input.value = "";
    status.textContent = "";
    await fetchProjects();
    selectProject(data.project_path);
  } catch (err) {
    status.className = "status error";
    status.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}


document.getElementById("all-projects-item").addEventListener("click", () => selectProject(""));
document.getElementById("add-project-btn").addEventListener("click", addProject);
document.getElementById("add-project-path").addEventListener("keydown", (e) => {
  if (e.key === "Enter") addProject();
});
document.getElementById("logout-btn").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  window.location.href = "/";
});
document.getElementById("dispatch-send").addEventListener("click", sendDispatch);
document.getElementById("backfill-btn").addEventListener("click", backfillHistory);
document.getElementById("open-settings").addEventListener("click", openSettings);
document.getElementById("open-settings-inline").addEventListener("click", openSettings);
document.getElementById("close-settings").addEventListener("click", closeSettings);
document.querySelector("[data-close-settings]").addEventListener("click", closeSettings);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeSettings();
});
document.getElementById("telemetry-explain-toggle").addEventListener("click", () => {
  toggleTelemetryExplainer();
});
document.getElementById("context-tab-entries").addEventListener("click", () => {
  state.contextTab = "entries";
  renderContext();
});
document.getElementById("context-tab-map").addEventListener("click", () => {
  state.contextTab = "map";
  renderContext();
});
document.getElementById("context-tab-preview").addEventListener("click", () => {
  state.contextTab = "preview";
  renderContext();
});
document.getElementById("context-help-toggle").addEventListener("click", (event) => {
  const help = document.getElementById("context-help");
  help.hidden = !help.hidden;
  event.currentTarget.setAttribute("aria-expanded", String(!help.hidden));
});
document.getElementById("context-search").addEventListener("input", (event) => {
  state.contextSearch = event.target.value;
  renderContext();
});
document.getElementById("context-state-filter").addEventListener("change", (event) => {
  state.contextStateFilter = event.target.value;
  renderContext();
});
document.getElementById("context-add-note").addEventListener("click", () => {
  const form = document.getElementById("context-note-form");
  form.hidden = false;
  document.getElementById("context-note-content").focus();
});
document.getElementById("context-note-cancel").addEventListener("click", () => {
  document.getElementById("context-note-form").hidden = true;
  document.getElementById("context-note-content").value = "";
});
document.getElementById("context-note-save").addEventListener("click", saveContextNote);
document.getElementById("context-recategorize-toggle").addEventListener("click", () => {
  const form = document.getElementById("context-recategorize-form");
  form.hidden = false;
  document.getElementById("context-recategorize-instructions").focus();
});
document.getElementById("context-recategorize-cancel").addEventListener("click", () => {
  document.getElementById("context-recategorize-form").hidden = true;
  document.getElementById("context-recategorize-instructions").value = "";
});
document.getElementById("context-recategorize-send").addEventListener("click", sendRecategorize);
document.getElementById("context-limit").addEventListener("change", (event) => {
  mutateContext(
    "/api/context/settings",
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_path: state.currentProject,
        recent_limit: Number(event.target.value),
      }),
    },
    "Failed to update the recent-entry limit."
  );
});
document.getElementById("context-copy").addEventListener("click", async () => {
  if (!state.contextData) return;
  try {
    await navigator.clipboard.writeText(state.contextData.preview || "");
    showToast("Exact shared agent briefing copied.", false);
  } catch {
    setContextStatus("Clipboard access was denied by the browser.", true);
  }
});
document.querySelectorAll(".preset").forEach((btn) =>
  btn.addEventListener("click", () => applyPreset(btn.dataset.preset))
);
document.getElementById("dispatch-file").addEventListener("change", (e) => {
  if (e.target.files && e.target.files[0]) uploadFile(e.target.files[0]);
  e.target.value = "";
});

fetchProjects();
selectProject("");
pollActiveAgents();
loadInteractions();
loadAgentStatus();
setInterval(fetchProjects, 6000);
setInterval(pollEvents, 4000);
setInterval(pollConflicts, 5000);
setInterval(pollActiveAgents, 2500);
setInterval(refreshAgentElapsedTimes, 1000);
setInterval(() => {
  if (document.visibilityState === "visible") loadInteractions();
}, 2000);
setInterval(() => {
  if (state.currentProject && document.visibilityState === "visible") loadTelemetry();
}, 5000);
setInterval(() => {
  if (state.currentProject && (runningDispatchJobs.size > 0 || state.expandedJobs.size > 0)) {
    loadDispatchHistory();
  }
}, 2000);
