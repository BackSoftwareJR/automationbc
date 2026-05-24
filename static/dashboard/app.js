const API_KEY_STORAGE = "bridge_api_key";
const WS_URL = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/dashboard`;
const POLL_OFFLINE_MS = 45000;
const PING_INTERVAL_MS = 25000;

let apiKey = sessionStorage.getItem(API_KEY_STORAGE) || "";
let ws = null;
let tasks = new Map();
let projectQueues = { projects: [], project_count: 0, waiting_total: 0, active_projects: 0 };
let selectedRunId = null;
let reconnectTimer = null;
let pingTimer = null;
let pollTimer = null;
let clockTimer = null;
let wsConnected = false;
let bootPlayed = false;
let renderFrame = null;
let renderParts = { stats: false, queues: false, active: false, list: false };

const $ = (id) => document.getElementById(id);

const BOOT_LINES = [
  { text: "BACKCLUB AGENT BRIDGE v2.1", cls: "boot-ok" },
  { text: "─────────────────────────────────", cls: "" },
  { text: "[ OK ] kernel module loaded", cls: "boot-ok" },
  { text: "[ OK ] cursor-cli bridge ready", cls: "boot-ok" },
  { text: "[ OK ] websocket hub initialized", cls: "boot-ok" },
  { text: "[ OK ] task registry online", cls: "boot-ok" },
  { text: "[ .. ] awaiting auth token...", cls: "boot-warn" },
];

function show(el) { el.classList.remove("hidden"); }
function hide(el) { el.classList.add("hidden"); }

function setStatus(msg, active = false) {
  const el = $("status-message");
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle("active", active);
}

function statusClass(status) {
  return `status-chip status-${status}`;
}

function formatDuration(ms) {
  if (!ms) return "—";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
}

function formatTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("it-IT");
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function pulseStat(key) {
  const tile = document.querySelector(`.stat-tile[data-stat="${key}"]`);
  if (!tile) return;
  tile.classList.remove("pulse");
  void tile.offsetWidth;
  tile.classList.add("pulse");
}

function setText(id, value) {
  const el = $(id);
  if (el) el.textContent = value;
}

function computeStats() {
  const stats = {
    total: 0,
    queue_waiting: 0,
    queued: 0,
    running: 0,
    retrying: 0,
    success: 0,
    error: 0,
    cancelled: 0,
  };
  tasks.forEach((t) => {
    stats.total++;
    if (stats[t.status] !== undefined) stats[t.status]++;
  });
  return stats;
}

function updateStats(stats) {
  if (!stats) stats = computeStats();
  const prev = {
    total: parseInt($("stat-total")?.textContent || "0", 10) || 0,
    running: parseInt($("stat-running")?.textContent || "0", 10) || 0,
    success: parseInt($("stat-success")?.textContent || "0", 10) || 0,
    error: parseInt($("stat-error")?.textContent || "0", 10) || 0,
  };
  const running = (stats.running ?? 0) + (stats.retrying ?? 0);
  setText("stat-total", stats.total ?? 0);
  setText("stat-running", running);
  setText("stat-success", stats.success ?? 0);
  setText("stat-error", stats.error ?? 0);
  setText("stat-queue-waiting", stats.queue_waiting ?? 0);
  setText("stat-queued", stats.queued ?? 0);
  setText("stat-cancelled", stats.cancelled ?? 0);
  if ((stats.total ?? 0) !== prev.total) pulseStat("total");
  if (running !== prev.running) pulseStat("running");
  if ((stats.success ?? 0) !== prev.success) pulseStat("success");
  if ((stats.error ?? 0) !== prev.error) pulseStat("error");
}

function mergeTask(existing, incoming) {
  if (!existing) return incoming;
  if (!incoming) return existing;
  const merged = { ...existing, ...incoming };
  const existingLogs = existing.logs || [];
  const incomingLogs = incoming.logs || [];
  merged.logs = incomingLogs.length >= existingLogs.length ? incomingLogs : existingLogs;
  return merged;
}

function scheduleRender(parts = "all") {
  if (parts === "all") {
    renderParts = { stats: true, queues: true, active: true, list: true };
  } else if (typeof parts === "object") {
    Object.assign(renderParts, parts);
  }
  if (renderFrame !== null) return;
  renderFrame = requestAnimationFrame(() => {
    renderFrame = null;
    const p = { ...renderParts };
    renderParts = { stats: false, queues: false, active: false, list: false };
    if (p.stats) updateStats();
    if (p.queues) renderProjectQueues();
    if (p.active) renderActiveRuns();
    if (p.list) renderTaskList();
  });
}

function flashTaskRow(runId) {
  requestAnimationFrame(() => {
    const row = $("task-list")?.querySelector(`[data-run-id="${runId}"]`);
    if (!row) return;
    row.classList.remove("flash");
    void row.offsetWidth;
    row.classList.add("flash");
  });
}

function patchTask(runId, partial, opts = {}) {
  if (!runId) return;
  const isNew = !tasks.has(runId);
  const existing = tasks.get(runId);
  tasks.set(runId, mergeTask(existing, { run_id: runId, ...partial }));
  scheduleRender({ stats: true, queues: true, active: true, list: true });
  if (isNew && opts.flash) flashTaskRow(runId);
  if (selectedRunId === runId) renderDrawer(tasks.get(runId));
}

function upsertTask(task, opts = {}) {
  if (!task?.run_id) return;
  patchTask(task.run_id, task, opts);
}

function appendDrawerLogLine(log) {
  const logs = $("drawer-logs");
  if (!logs) return;
  const line = document.createElement("div");
  line.className = `log-line-${log.stream}`;
  line.textContent = `[${new Date(log.ts).toLocaleTimeString("it-IT")}] [${log.stream}] ${log.line}`;
  logs.appendChild(line);
  logs.scrollTop = logs.scrollHeight;
}

function appendLog(runId, log) {
  if (!runId || !log) return;
  const existing = tasks.get(runId) || { run_id: runId, logs: [], status: "running" };
  existing.logs = existing.logs || [];
  const last = existing.logs[existing.logs.length - 1];
  if (last && last.ts === log.ts && last.line === log.line) return;
  existing.logs = [...existing.logs, log];
  tasks.set(runId, existing);
  if (selectedRunId === runId) appendDrawerLogLine(log);
}

function applyQueuesData(queues) {
  if (!queues) return;
  projectQueues = queues;
  (queues.projects || []).forEach((pq) => {
    (pq.items || []).forEach((item) => {
      const existing = tasks.get(item.run_id);
      if (existing) {
        tasks.set(
          item.run_id,
          mergeTask(existing, {
            status: item.status,
            queue_position: item.position,
            task_id: item.task_id,
            prompt_preview: item.prompt_preview || existing.prompt_preview,
          })
        );
      }
    });
  });
}

function applySnapshot(data) {
  tasks.clear();
  (data?.tasks || []).forEach((t) => tasks.set(t.run_id, t));
  if (data?.project_queues) applyQueuesData(data.project_queues);
  updateStats(data?.stats || computeStats());
  scheduleRender("all");
  if (selectedRunId && tasks.has(selectedRunId)) {
    renderDrawer(tasks.get(selectedRunId));
  }
}

function getFilteredTasks() {
  const statusFilter = $("filter-status")?.value || "";
  const projectFilter = $("filter-project")?.value?.trim() || "";
  return [...tasks.values()]
    .filter((t) => !statusFilter || t.status === statusFilter)
    .filter((t) => !projectFilter || String(t.project_id).includes(projectFilter))
    .sort((a, b) => {
      const ta = a.started_at || a.finished_at || "";
      const tb = b.started_at || b.finished_at || "";
      return tb.localeCompare(ta);
    });
}

function renderProjectQueues() {
  const container = $("project-queues");
  const summary = $("queues-summary");
  if (!container) return;

  const projects = projectQueues?.projects || [];

  if (summary) {
    const waiting = projectQueues?.waiting_total ?? 0;
    const count = projectQueues?.project_count ?? projects.length;
    summary.textContent = `${count} progetti · ${waiting} in attesa`;
  }

  container.innerHTML = "";
  const noQueues = $("no-queues");
  if (projects.length === 0) {
    if (noQueues) show(noQueues);
    return;
  }
  if (noQueues) hide(noQueues);

  projects.forEach((pq) => {
    const card = document.createElement("div");
    card.className = "project-queue-card" + (pq.active_run_id ? " has-active" : "");
    const waitingItems = (pq.items || []).filter((i) => i.status === "queue_waiting");
    const activeItem = (pq.items || []).find((i) => i.run_id === pq.active_run_id);

    let activeHtml = "";
    if (activeItem) {
      activeHtml = `
        <div class="pq-active" data-run-id="${activeItem.run_id}">
          <span class="${statusClass(activeItem.status)}">${activeItem.status}</span>
          · task:${activeItem.task_id}
          <div class="pq-item-preview">${escapeHtml(activeItem.prompt_preview || "")}</div>
        </div>`;
    } else if (pq.active_run_id) {
      activeHtml = `<div class="pq-active">task:${pq.active_task_id ?? "?"} · in esecuzione</div>`;
    }

    const waitingHtml = waitingItems.length
      ? `<div class="pq-waiting-label">IN CODA (${waitingItems.length})</div>
         <ul class="pq-queue-list">
           ${waitingItems.map((item) => `
             <li class="pq-queue-item" data-run-id="${item.run_id}">
               <span class="pq-pos">#${item.position}</span>
               <div class="pq-item-body">
                 <div class="pq-item-title">task:${item.task_id}</div>
                 <div class="pq-item-preview">${escapeHtml(item.prompt_preview || "")}</div>
               </div>
               <span class="${statusClass(item.status)}">${item.status}</span>
             </li>`).join("")}
         </ul>`
      : `<div class="pq-waiting-label">coda vuota</div>`;

    card.innerHTML = `
      <div class="pq-header">
        <span class="pq-title">proj:${pq.project_id}</span>
        <span class="status-chip">${pq.total_count} in pipeline</span>
      </div>
      <div class="pq-meta">${escapeHtml(pq.project_area || "—")}${pq.github_url ? ` · ${escapeHtml(pq.github_url)}` : ""}</div>
      ${activeHtml}
      ${waitingHtml}
    `;

    card.querySelectorAll("[data-run-id]").forEach((el) => {
      el.addEventListener("click", () => openDrawer(el.dataset.runId));
    });

    container.appendChild(card);
  });
}

function renderActiveRuns() {
  const container = $("active-runs");
  if (!container) return;
  const active = [...tasks.values()].filter((t) =>
    ["queue_waiting", "queued", "running", "retrying"].includes(t.status)
  );
  container.innerHTML = "";
  if (active.length === 0) {
    show($("no-active"));
    return;
  }
  hide($("no-active"));
  active.forEach((task) => {
    const card = document.createElement("div");
    card.className = "run-card";
    card.innerHTML = `
      <div class="run-card-header">
        <h3>task:${task.task_id} · proj:${task.project_id}</h3>
        <span class="${statusClass(task.status)}">${task.status}</span>
      </div>
      <p>${escapeHtml(task.project_area)} · attempt ${task.attempt}/${task.max_attempts}${task.queue_position ? ` · coda #${task.queue_position}/${task.queue_total || "?"}` : ""}</p>
      <p>${escapeHtml(task.prompt_preview || "")}</p>
      <div style="display:flex;gap:8px;margin-top:10px">
        <button class="btn btn-ghost" data-action="view" data-id="${task.run_id}">inspect</button>
        <button class="btn btn-danger" data-action="cancel" data-id="${task.run_id}">halt</button>
      </div>
    `;
    container.appendChild(card);
  });
  container.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const id = btn.dataset.id;
      if (btn.dataset.action === "view") openDrawer(id);
      if (btn.dataset.action === "cancel") cancelTask(id);
    });
  });
}

function renderTaskList() {
  const list = $("task-list");
  if (!list) return;
  const filtered = getFilteredTasks();
  list.innerHTML = "";
  filtered.forEach((task) => {
    const row = document.createElement("div");
    row.className = "task-row" + (selectedRunId === task.run_id ? " selected" : "");
    row.dataset.runId = task.run_id;
    row.innerHTML = `
      <div class="task-row-top">
        <span class="task-row-title">task:${task.task_id} · proj:${task.project_id}</span>
        <span class="${statusClass(task.status)}">${task.status}</span>
      </div>
      <div class="task-row-meta">
        ${escapeHtml(task.project_area)} · ${formatDuration(task.duration_ms)} · ${formatTime(task.started_at)}${task.queue_position ? ` · #${task.queue_position}/${task.queue_total}` : ""}
      </div>
    `;
    row.addEventListener("click", () => openDrawer(task.run_id));
    list.appendChild(row);
  });
}

function renderDrawer(task) {
  if (!task) return;
  $("drawer-title").textContent = `task:${task.task_id} · run:${task.run_id.slice(0, 8)}`;
  $("drawer-meta").innerHTML = `
    <div><strong>status</strong> <span class="${statusClass(task.status)}">${task.status}</span></div>
    <div><strong>project</strong> ${task.project_id} · <strong>area</strong> ${escapeHtml(task.project_area)}</div>
    <div><strong>workspace</strong> ${escapeHtml(task.workspace_path || "—")}</div>
    <div><strong>github</strong> ${escapeHtml(task.github_url || "—")}</div>
    <div><strong>website</strong> ${escapeHtml(task.website_url || "—")}</div>
    <div><strong>started</strong> ${formatTime(task.started_at)} · <strong>duration</strong> ${formatDuration(task.duration_ms)}</div>
    <div><strong>exit</strong> ${task.exit_code ?? "—"}</div>
    ${task.failure_code ? `<div><strong>failure</strong> <code>${escapeHtml(task.failure_code)}</code></div>` : ""}
    ${task.queue_position ? `<div><strong>coda</strong> #${task.queue_position} / ${task.queue_total ?? "?"}</div>` : ""}
    <div style="margin-top:10px"><strong>prompt</strong><br>${escapeHtml(task.prompt_preview || "")}</div>
  `;
  const actions = $("drawer-actions");
  actions.innerHTML = "";
  if (["queue_waiting", "queued", "running", "retrying"].includes(task.status)) {
    const cancelBtn = document.createElement("button");
    cancelBtn.className = "btn btn-danger";
    cancelBtn.textContent = task.status === "queue_waiting" ? "[ RIMUOVI DA CODA ]" : "[ HALT ]";
    cancelBtn.onclick = () => cancelTask(task.run_id);
    actions.appendChild(cancelBtn);
  }
  if (["error", "cancelled"].includes(task.status)) {
    const retryBtn = document.createElement("button");
    retryBtn.className = "btn btn-primary";
    retryBtn.textContent = "[ RETRY ]";
    retryBtn.onclick = () => retryTask(task.run_id);
    actions.appendChild(retryBtn);
  }
  const logs = $("drawer-logs");
  logs.innerHTML = "";
  (task.logs || []).forEach((log) => appendDrawerLogLine(log));
  const result = $("drawer-result");
  if (task.result_summary) {
    result.textContent = task.result_summary;
    show(result);
  } else if (task.error_message) {
    const prefix = task.failure_code ? `[${task.failure_code}] ` : "";
    result.textContent = prefix + task.error_message;
    show(result);
  } else {
    hide(result);
  }
}

function openDrawer(runId) {
  selectedRunId = runId;
  const task = tasks.get(runId);
  if (task) renderDrawer(task);
  show($("drawer"));
  show($("drawer-backdrop"));
  scheduleRender({ list: true });
  fetchTaskDetail(runId);
}

function closeDrawer() {
  selectedRunId = null;
  hide($("drawer"));
  hide($("drawer-backdrop"));
  scheduleRender({ list: true });
  setStatus(wsConnected ? "sync live — monitoring agents" : "sync offline");
}

async function apiFetch(path, options = {}) {
  const headers = { "X-API-Key": apiKey, ...(options.headers || {}) };
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "Request failed");
  }
  return res.json();
}

async function fetchSnapshot() {
  try {
    applySnapshot(await apiFetch("/api/dashboard/snapshot"));
  } catch (e) {
    console.error("Snapshot poll failed", e);
    const msg = typeof e.message === "string" ? e.message : "snapshot failed";
    if (msg.includes("401") || String(msg).toLowerCase().includes("api")) {
      sessionStorage.removeItem(API_KEY_STORAGE);
      apiKey = "";
      showLogin("API key non valida — reinseriscila");
      return;
    }
    if (!wsConnected) setStatus(`sync offline — ${msg}`);
  }
}

async function fetchTaskDetail(runId) {
  try {
    upsertTask(await apiFetch(`/api/dashboard/tasks/${runId}`));
  } catch (e) {
    console.error(e);
  }
}

async function cancelTask(runId) {
  try {
    setStatus(`sending halt signal to ${runId.slice(0, 8)}…`, true);
    await apiFetch(`/api/dashboard/tasks/${runId}/cancel`, { method: "POST" });
    setStatus("halt signal sent");
  } catch (e) {
    alert(e.message);
    setStatus(`halt failed: ${e.message}`);
  }
}

async function retryTask(runId) {
  try {
    setStatus(`relaunching task from ${runId.slice(0, 8)}…`, true);
    const res = await apiFetch(`/api/dashboard/tasks/${runId}/retry`, { method: "POST" });
    openDrawer(res.new_run_id);
    setStatus(`retry queued — new run ${res.new_run_id.slice(0, 8)}`, true);
  } catch (e) {
    alert(e.message);
    setStatus(`retry failed: ${e.message}`);
  }
}

function setWsStatus(live) {
  wsConnected = live;
  const badge = $("ws-status");
  const label = $("ws-label");
  badge?.classList.toggle("live", live);
  badge?.classList.toggle("offline", !live);
  if (label) label.textContent = live ? "SYNC_LIVE" : "SYNC_OFFLINE";
  setStatus(live ? "sync live — monitoring agents" : "sync offline");
  if (live) stopOfflinePoll();
  else startOfflinePoll();
}

function handleWsMessage(msg) {
  if (msg.stats) updateStats(msg.stats);
  switch (msg.type) {
    case "auth_ok":
      setWsStatus(true);
      break;
    case "auth_error":
      setWsStatus(false);
      showLogin(msg.message);
      break;
    case "snapshot":
      applySnapshot(msg.data);
      break;
    case "task_created":
      if (msg.task) {
        upsertTask(msg.task, { flash: true });
        setStatus(`new task ${msg.task.task_id} · project ${msg.task.project_id}`, true);
      }
      break;
    case "task_updated":
      if (msg.task) upsertTask(msg.task);
      break;
    case "task_finished":
      if (msg.task) {
        upsertTask(msg.task);
        setStatus(`task ${msg.task.task_id} finished — ${msg.task.status}`, msg.task.status === "success");
      }
      break;
    case "task_cancelled":
      if (msg.task) upsertTask(msg.task);
      break;
    case "callback_updated":
      if (msg.task) patchTask(msg.task.run_id, msg.task);
      break;
    case "queue_updated":
      if (msg.queues) applyQueuesData(msg.queues);
      if (msg.stats) updateStats(msg.stats);
      scheduleRender({ stats: true, queues: true, active: true, list: true });
      break;
    case "log_line":
      if (msg.run_id && msg.log) appendLog(msg.run_id, msg.log);
      break;
    default:
      break;
  }
}

function startOfflinePoll() {
  stopOfflinePoll();
  pollTimer = setTimeout(async function tick() {
    if (!wsConnected) {
      await fetchSnapshot();
      pollTimer = setTimeout(tick, POLL_OFFLINE_MS);
    }
  }, POLL_OFFLINE_MS);
}

function stopOfflinePoll() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function startPing() {
  stopPing();
  pingTimer = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send("ping");
  }, PING_INTERVAL_MS);
}

function stopPing() {
  if (pingTimer) {
    clearInterval(pingTimer);
    pingTimer = null;
  }
}

function startClock() {
  stopClock();
  const tick = () => {
    const el = $("live-clock");
    if (el) el.textContent = new Date().toLocaleTimeString("it-IT", { hour12: false });
  };
  tick();
  clockTimer = setInterval(tick, 1000);
}

function stopClock() {
  if (clockTimer) {
    clearInterval(clockTimer);
    clockTimer = null;
  }
}

function connectWs() {
  if (ws) {
    ws.close();
    ws = null;
  }
  ws = new WebSocket(WS_URL);
  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "auth", api_key: apiKey }));
    startPing();
    setStatus("websocket open — authenticating…", true);
  };
  ws.onmessage = (event) => {
    try {
      handleWsMessage(JSON.parse(event.data));
    } catch (e) {
      console.error("WS parse error", e);
    }
  };
  ws.onclose = () => {
    setWsStatus(false);
    stopPing();
    reconnectTimer = setTimeout(connectWs, 3000);
  };
  ws.onerror = () => setWsStatus(false);
}

async function playBootSequence() {
  if (bootPlayed) {
    show($("login-form"));
    return;
  }
  bootPlayed = true;
  const log = $("boot-log");
  if (!log) return;
  log.innerHTML = "";
  hide($("login-form"));
  for (const line of BOOT_LINES) {
    await new Promise((r) => setTimeout(r, 180 + Math.random() * 120));
    const span = document.createElement("span");
    if (line.cls) span.className = line.cls;
    span.textContent = line.text + "\n";
    log.appendChild(span);
    log.scrollTop = log.scrollHeight;
  }
  await new Promise((r) => setTimeout(r, 400));
  show($("login-form"));
}

function showApp() {
  hide($("login-screen"));
  show($("app"));
  startClock();
  connectWs();
}

function showLogin(error) {
  show($("login-screen"));
  hide($("app"));
  stopOfflinePoll();
  stopPing();
  stopClock();
  if (ws) ws.close();
  clearTimeout(reconnectTimer);
  playBootSequence();
  if (error) {
    $("login-error").textContent = error;
    show($("login-error"));
  } else hide($("login-error"));
}

$("login-btn")?.addEventListener("click", () => {
  apiKey = $("api-key-input").value.trim();
  if (!apiKey) return;
  sessionStorage.setItem(API_KEY_STORAGE, apiKey);
  showApp();
});

$("api-key-input")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("login-btn")?.click();
});

$("logout-btn")?.addEventListener("click", () => {
  sessionStorage.removeItem(API_KEY_STORAGE);
  apiKey = "";
  if (ws) ws.close();
  clearTimeout(reconnectTimer);
  stopOfflinePoll();
  stopPing();
  stopClock();
  showLogin();
});

$("drawer-close")?.addEventListener("click", closeDrawer);
$("drawer-backdrop")?.addEventListener("click", closeDrawer);
$("filter-status")?.addEventListener("change", () => scheduleRender({ list: true }));
$("filter-project")?.addEventListener("input", () => scheduleRender({ list: true }));

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("drawer").classList.contains("hidden")) closeDrawer();
});

function bootDashboard() {
  try {
    if (apiKey) showApp();
    else showLogin();
  } catch (err) {
    console.error("boot failed", err);
    show($("login-screen"));
    hide($("app"));
    const errBox = $("login-error");
    if (errBox) {
      errBox.textContent = `Errore UI: ${err.message}. Premi Ctrl+F5.`;
      show(errBox);
    }
  }
}

bootDashboard();
