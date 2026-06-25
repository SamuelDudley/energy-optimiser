// /ops dashboard tab.
//
// Lazy: nothing is fetched until the user clicks the "Ops" tab. Polling
// only runs while the tab is visible; tabbing away or backgrounding the
// page pauses it (document.visibilityState). Each panel polls at its
// own cadence — solve histogram every 60 s (slow-changing), modbus +
// api health every 30 s (matches the server-side TTL cache).
//
// uPlot charts (ops-solve-series, ops-solve-histogram, ops-solve-status,
// ops-modbus-writes) are built lazily on first tab show, AFTER the
// hidden=false flip, so they never build at width=0 (spec §11 risk #6).

import { isNarrow, onBreakpointChange, registry } from "./chart-core.js";
import { buildOpsCharts } from "./ops-charts.js";

const POLL_MS_FAST = 30_000; // matches server cache TTL
const POLL_MS_SLOW = 60_000;

const opsState = {
  activeTab: "energy",
  windowH: 1,
  pollers: [],   // [{ id, intervalMs, fn }]
  timers: [],    // setInterval handles, cleared on tab-away
  booted: false,
  // uPlot chart controller — null until first Ops tab show
  charts: null,
};

// ── DOM helpers ──────────────────────────────────────────────────

function $(id) { return document.getElementById(id); }
function setOpsStatus(msg) { const el = $("ops-status"); if (el) el.textContent = msg; }

// ── Auth: reuse the bearer token from dashboard.js (same localStorage key)
const TOKEN_LS_KEY = "eo_dashboard_token";

async function opsFetch(path) {
  const token = localStorage.getItem(TOKEN_LS_KEY);
  if (!token) throw new Error("no token — open the Energy tab first to enter one");
  const res = await fetch(path, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} on ${path}`);
  return res.json();
}

function opsUrl(endpoint) {
  return `/ops/${endpoint}?window_h=${opsState.windowH}`;
}

// ── Panel: LP solve performance ──────────────────────────────────

async function refreshSolve() {
  let body;
  try {
    body = await opsFetch(opsUrl("solve"));
  } catch (e) {
    setOpsStatus(`solve: ${e.message}`);
    return;
  }
  if (!body || !Array.isArray(body.series)) return;

  // Ensure charts are built (may have been deferred if data arrived before tab show)
  ensureCharts();
  if (opsState.charts) {
    opsState.charts.updateSolve(body);
  }
}

// ── Panel: Modbus health ─────────────────────────────────────────

function fmtMs(v) {
  if (v == null) return "—";
  return `${Number(v).toFixed(1)} ms`;
}
function fmtN(v) { return v == null ? "—" : String(v); }

async function refreshModbus() {
  let body;
  try {
    body = await opsFetch(opsUrl("modbus"));
  } catch (e) {
    setOpsStatus(`modbus: ${e.message}`);
    return;
  }
  const reads = body.reads || {};
  const incidents = body.incidents || {};
  const summary = $("ops-modbus-summary");
  summary.innerHTML = `
    <div class="ops-cell"><div class="ops-cell-label">Read batches</div><div class="ops-cell-value">${fmtN(reads.batches)}</div></div>
    <div class="ops-cell"><div class="ops-cell-label">p50 / p95</div><div class="ops-cell-value">${fmtMs(reads.p50_ms)} / ${fmtMs(reads.p95_ms)}</div></div>
    <div class="ops-cell"><div class="ops-cell-label">Reads</div><div class="ops-cell-value">${fmtN(reads.total_reads)}</div></div>
    <div class="ops-cell"><div class="ops-cell-label">Read errors</div><div class="ops-cell-value ${reads.total_read_errors > 0 ? "warn" : ""}">${fmtN(reads.total_read_errors)}</div></div>
    <div class="ops-cell"><div class="ops-cell-label">Reconnect ticks</div><div class="ops-cell-value ${reads.reconnect_ticks > 0 ? "warn" : ""}">${fmtN(reads.reconnect_ticks)}</div></div>
    <div class="ops-cell"><div class="ops-cell-label">Grid sensor offline</div><div class="ops-cell-value ${reads.grid_sensor_offline_ticks > 0 ? "warn" : ""}">${fmtN(reads.grid_sensor_offline_ticks)}</div></div>
    <div class="ops-cell"><div class="ops-cell-label">Verify deviations</div><div class="ops-cell-value ${(incidents.verify_deviation || 0) > 0 ? "warn" : ""}">${fmtN(incidents.verify_deviation || 0)}</div></div>
    <div class="ops-cell"><div class="ops-cell-label">Reconnects</div><div class="ops-cell-value">${fmtN(incidents.modbus_reconnected || 0)}</div></div>
  `;

  // Per-register write success/error breakdown — uPlot chart
  ensureCharts();
  if (opsState.charts) {
    opsState.charts.updateModbus(body);
  }
}

// ── Panel: API client health ─────────────────────────────────────

async function refreshApiHealth() {
  let body;
  try {
    body = await opsFetch(opsUrl("api_health"));
  } catch (e) {
    setOpsStatus(`api_health: ${e.message}`);
    return;
  }
  const clients = body.clients || [];
  if (clients.length === 0) {
    $("ops-api-table").innerHTML = '<div class="muted">no API calls in window</div>';
    return;
  }
  const rows = clients.map(c => {
    const errPct = c.calls > 0 ? (100 * c.errors / c.calls).toFixed(1) : "0.0";
    const errClass = c.errors > 0 ? "warn" : "";
    return `<tr>
      <td>${c.client}</td>
      <td>${c.calls}</td>
      <td class="${errClass}">${c.errors} (${errPct}%)</td>
      <td>${fmtMs(c.p50_ms)}</td>
      <td>${fmtMs(c.p95_ms)}</td>
      <td>${fmtMs(c.max_ms)}</td>
      <td class="muted">${c.last_call_ts ? c.last_call_ts.slice(11, 19) : "—"}</td>
    </tr>`;
  }).join("");
  $("ops-api-table").innerHTML = `
    <table class="ops-table">
      <thead><tr>
        <th>client</th><th>calls</th><th>errors</th>
        <th>p50</th><th>p95</th><th>max</th><th>last</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

// ── Panel: state machine + incidents list ────────────────────────

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// Pull the most-useful fields from each event payload so the list
// reads as one line per row instead of dumping the full JSON. Falls
// back to a compact JSON for unrecognised event types.
function summariseEvent(e) {
  const d = e.data || {};
  switch (e.event) {
    case "state_transition": {
      const from = d.from || "?";
      const to = d.to || "?";
      const reason = d.reason || "";
      return reason ? `${from} → ${to}  (${reason})` : `${from} → ${to}`;
    }
    case "fallback_engaged":
      return d.reason ? `reason: ${d.reason}` : "engaged";
    case "circuit_breaker_open":
      return d.reason ? `open — ${d.reason}` : "open";
    case "circuit_breaker_closed":
      return "closed";
    case "export_blocked_stale_price":
      return d.age_s != null ? `price age ${d.age_s}s` : "stale price";
    default: {
      const keys = Object.keys(d);
      if (keys.length === 0) return "";
      const compact = keys.slice(0, 4).map(k => `${k}=${JSON.stringify(d[k])}`).join("  ");
      return keys.length > 4 ? compact + "  …" : compact;
    }
  }
}

async function refreshState() {
  let body;
  try {
    body = await opsFetch(opsUrl("state"));
  } catch (e) {
    setOpsStatus(`state: ${e.message}`);
    return;
  }
  const events = body.events || [];
  if (events.length === 0) {
    $("ops-state-list").innerHTML = '<li class="muted">no state events in window</li>';
    return;
  }
  // Newest first for the list view
  const items = events.slice().reverse().map(e => {
    const ts = e.ts ? e.ts.slice(11, 19) : "—";
    const summary = summariseEvent(e);
    return `<li><span class="ev-ts">${escapeHtml(ts)}</span>` +
           `<span class="ev-name">${escapeHtml(e.event || "")}</span>` +
           `<span class="ev-data">${escapeHtml(summary)}</span></li>`;
  }).join("");
  $("ops-state-list").innerHTML = items;
}

// ── Chart lifecycle ──────────────────────────────────────────────

/**
 * Build the uPlot chart controllers if they haven't been built yet.
 * Must only be called AFTER the ops tab is visible (hidden=false) so
 * chart containers have non-zero pixel dimensions (spec §11 risk #6).
 */
function ensureCharts() {
  if (opsState.charts) return;  // already built
  // Guard: only build when the ops-view is actually visible
  const view = $("ops-view");
  if (!view || view.hidden) return;
  opsState.charts = buildOpsCharts();
}

// ── Polling orchestration ────────────────────────────────────────

function stopPollers() {
  opsState.timers.forEach(t => clearInterval(t));
  opsState.timers = [];
}

async function refreshAll() {
  setOpsStatus("refreshing…");
  await Promise.all([
    refreshSolve(),
    refreshModbus(),
    refreshApiHealth(),
    refreshState(),
  ]);
  setOpsStatus(`updated ${new Date().toLocaleTimeString()}`);
}

function startPollers() {
  stopPollers();
  // Solve panel: per-tick line chart redraws faster than the others
  opsState.timers.push(setInterval(refreshSolve, POLL_MS_SLOW));
  opsState.timers.push(setInterval(refreshModbus, POLL_MS_FAST));
  opsState.timers.push(setInterval(refreshApiHealth, POLL_MS_FAST));
  opsState.timers.push(setInterval(refreshState, POLL_MS_FAST));
}

// ── Tab switching ────────────────────────────────────────────────

function showTab(name) {
  opsState.activeTab = name;
  document.querySelectorAll(".tab-btn").forEach(btn => {
    const active = btn.dataset.tab === name;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
  $("energy-view").hidden = name !== "energy";
  $("ops-view").hidden = name !== "ops";
  // The cursor strip (decision/mode + panel-values-at-cursor) is specific to
  // the Energy time-series; hide it on Ops so the ops charts get the space.
  document.body.classList.toggle("ops-active", name === "ops");

  if (name === "ops") {
    // First visit: do an immediate refresh and start the timers.
    // Charts are built lazily inside refreshSolve/refreshModbus (via ensureCharts),
    // which run AFTER the hidden=false flip above. The 50ms setTimeout below
    // then resizes all registered instances so they fit the now-visible container.
    refreshAll();
    startPollers();
  } else {
    stopPollers();
  }

  // Force every uPlot chart in the now-visible tab to re-fit its container.
  // The 50ms delay lets the browser apply the hidden flip before resize runs.
  setTimeout(() => {
    registry.resizeAll();
    // Also call our ops-specific resize in case some instances aren't in
    // the global registry yet (buildOpsCharts ensures they are, but belt+braces).
    if (opsState.charts && name === "ops") opsState.charts.resize();
  }, 50);
}

function installWindowButtons() {
  document.querySelectorAll(".ops-window-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".ops-window-btn").forEach(b => {
        b.classList.toggle("primary", b === btn);
        b.setAttribute("aria-pressed", b === btn ? "true" : "false");
      });
      opsState.windowH = Number(btn.dataset.windowH || 1);
      if (opsState.activeTab === "ops") refreshAll();
    });
  });
}

function installTabBar() {
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => showTab(btn.dataset.tab));
  });
}

// Mobile-only swipe-left tab cycle, scoped to the top status strip
// so it never competes with vertical scrolling through long tab
// content or with horizontal scroll inside the ops tables.
// Swipe-RIGHT is intentionally not handled — browsers use that as
// the "back" gesture and we don't want to steal it. Swipe-left wraps
// around (last → first), so every tab is reachable from any other
// in at most N-1 swipes. Tab order is read from the DOM so adding a
// tab needs no code change here. Listeners are passive — vertical
// page-scroll is never blocked.
function installSwipeNav() {
  const strip = document.getElementById("status-strip");
  if (!strip) return;
  const tabs = Array.from(document.querySelectorAll(".tab-btn"))
    .map(b => b.dataset.tab)
    .filter(Boolean);
  if (tabs.length < 2) return;

  const MIN_DX = 50;     // px of horizontal travel to count
  const MAX_DY = 40;     // px of vertical travel ceiling — above = scroll
  const RATIO = 1.5;     // |dx| must beat |dy| by this factor
  const MAX_MS = 600;    // gesture must complete inside this window

  let startX = 0, startY = 0, startT = 0, active = false;

  strip.addEventListener("touchstart", (e) => {
    active = false;
    if (!isNarrow()) return;
    if (e.touches.length !== 1) return;
    const t = e.touches[0];
    startX = t.clientX;
    startY = t.clientY;
    startT = Date.now();
    active = true;
  }, { passive: true });

  strip.addEventListener("touchend", (e) => {
    if (!active) return;
    active = false;
    const t = e.changedTouches[0];
    const dx = t.clientX - startX;
    const dy = t.clientY - startY;
    if (Date.now() - startT > MAX_MS) return;
    if (-dx < MIN_DX) return;                      // swipe-left only
    if (Math.abs(dy) > MAX_DY) return;
    if (-dx < Math.abs(dy) * RATIO) return;

    const idx = tabs.indexOf(opsState.activeTab);
    if (idx < 0) return;
    showTab(tabs[(idx + 1) % tabs.length]);
  }, { passive: true });
}

function installVisibilityPause() {
  document.addEventListener("visibilitychange", () => {
    if (opsState.activeTab !== "ops") return;
    if (document.visibilityState === "visible") {
      // Refresh on return so the panels aren't stale, then resume.
      refreshAll();
      startPollers();
    } else {
      stopPollers();
    }
  });
}

function boot() {
  if (opsState.booted) return;
  opsState.booted = true;
  installTabBar();
  installSwipeNav();
  installWindowButtons();
  installVisibilityPause();
  // Re-render ops charts when the viewport crosses the mobile breakpoint
  // so margins flip cleanly without waiting for the next 30/60s poll.
  onBreakpointChange(() => {
    if (opsState.activeTab === "ops") refreshAll();
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
