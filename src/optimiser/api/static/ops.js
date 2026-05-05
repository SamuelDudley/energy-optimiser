// /ops dashboard tab.
//
// Lazy: nothing is fetched until the user clicks the "Ops" tab. Polling
// only runs while the tab is visible; tabbing away or backgrounding the
// page pauses it (document.visibilityState). Each panel polls at its
// own cadence — solve histogram every 60 s (slow-changing), modbus +
// api health every 30 s (matches the server-side TTL cache).

(function () {
  "use strict";

  const POLL_MS_FAST = 30_000; // matches server cache TTL
  const POLL_MS_SLOW = 60_000;

  const opsState = {
    activeTab: "energy",
    windowH: 1,
    pollers: [],   // [{ id, intervalMs, fn }]
    timers: [],    // setInterval handles, cleared on tab-away
    booted: false,
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

  // Theme constants — kept in sync with the energy charts in dashboard.js
  // so the two tabs feel like one product. Anything that diverges here
  // will read as "this is a different page" on the user's eye.
  const PANEL_BG = "#161b22";
  const PLOT_BG = "#0e1116";
  const GRID_COLOR = "#21262d";
  const TICK_COLOR = "#444c56";
  const TICK_FONT_COLOR = "#c9d1d9";
  const TEXT_COLOR = "#e8edf2";
  const LABEL_COLOR = "#7d8590";
  const FONT_FAMILY =
    'Inter, "Segoe UI Variable", "Segoe UI", ui-sans-serif, system-ui, ' +
    '-apple-system, Roboto, "Helvetica Neue", Arial, sans-serif';
  const HOVER_LABEL = {
    bgcolor: PANEL_BG,
    bordercolor: TICK_COLOR,
    font: { family: FONT_FAMILY, size: 12, color: TEXT_COLOR },
  };
  // Panel-label annotation in the same style energy charts use: bold
  // label + grey unit suffix, anchored top-left of the plot area.
  function panelLabel(label, units) {
    const text = units
      ? `<b>${label}</b>  <span style="color:${LABEL_COLOR}">${units}</span>`
      : `<b>${label}</b>`;
    return [{
      xref: "paper", yref: "paper",
      x: 0, y: 1,
      xanchor: "left", yanchor: "bottom",
      yshift: 2,
      text,
      showarrow: false,
      font: { family: FONT_FAMILY, size: 11, color: TICK_FONT_COLOR },
      align: "left",
    }];
  }
  function axis(extra) {
    return Object.assign({
      gridcolor: GRID_COLOR,
      zerolinecolor: GRID_COLOR,
      tickfont: { size: 12, color: TICK_FONT_COLOR },
      tickcolor: TICK_COLOR,
      ticklen: 3,
      automargin: true,
    }, extra || {});
  }
  function baseLayout(extra) {
    const narrow = window.eoChart.isNarrow();
    return Object.assign({
      paper_bgcolor: PANEL_BG,
      plot_bgcolor: PLOT_BG,
      font: { color: TEXT_COLOR, family: FONT_FAMILY, size: 12 },
      margin: narrow
        ? { t: 22, l: 36, r: 6,  b: 32 }
        : { t: 26, l: 48, r: 12, b: 36 },
      xaxis: axis(),
      yaxis: axis(),
      autosize: true,
      hoverlabel: HOVER_LABEL,
      // `dragmode: false` on narrow lets vertical touch-scroll over the
      // chart scroll the page instead of panning the axis. Default desktop
      // dragmode is "zoom" — fine on these mostly-categorical bar/scatter
      // charts where there's nothing meaningful to pan to.
      ...window.eoChart.mobileLayoutFragment({ desktopDrag: "zoom" }),
    }, extra || {});
  }
  // Ops charts hide the modebar entirely (mobileConfig only strips a few
  // buttons by default); merge the shared mobile defaults so scroll-zoom,
  // touch behaviour and doubleClick:false stay consistent with the energy
  // charts.
  const PLOT_CONFIG = window.eoChart.mobileConfig({ displayModeBar: false });

  async function refreshSolve() {
    let body;
    try {
      body = await opsFetch(opsUrl("solve"));
    } catch (e) {
      setOpsStatus(`solve: ${e.message}`);
      return;
    }
    if (!body || !Array.isArray(body.series)) return;

    // Time series — solve_time_ms per tick, coloured by status
    const byStatus = {};
    body.series.forEach(p => {
      const s = p.status || "unknown";
      if (!byStatus[s]) byStatus[s] = { x: [], y: [] };
      byStatus[s].x.push(p.ts);
      byStatus[s].y.push(p.ms);
    });
    const STATUS_COLOR = { optimal: "#3fb950", feasible: "#58a6ff", infeasible: "#f85149", unknown: "#8b949e" };
    const seriesTraces = Object.entries(byStatus).map(([status, pts]) => ({
      type: "scattergl",
      mode: "markers",
      name: status,
      x: pts.x,
      y: pts.y,
      marker: { size: 5, color: STATUS_COLOR[status] || "#58a6ff" },
    }));
    Plotly.react("ops-solve-series", seriesTraces, baseLayout({
      annotations: panelLabel("SOLVE TIME", "ms per tick"),
      yaxis: axis({ rangemode: "tozero" }),
      hovermode: "x unified",
      legend: { orientation: "h", y: -0.2, font: { size: 11, family: FONT_FAMILY } },
    }), PLOT_CONFIG);
    window.eoChart.registerPlot("ops-solve-series");

    // Histogram — bucketed counts
    const labels = (body.histogram || []).map(b => b.bucket);
    const counts = (body.histogram || []).map(b => b.count);
    Plotly.react("ops-solve-histogram", [{
      type: "bar",
      x: labels,
      y: counts,
      marker: { color: "#58a6ff" },
    }], baseLayout({
      annotations: panelLabel("DISTRIBUTION", "ticks"),
    }), PLOT_CONFIG);
    window.eoChart.registerPlot("ops-solve-histogram");

    // Status mix
    const sc = body.status_counts || {};
    const sks = Object.keys(sc);
    if (sks.length > 0) {
      Plotly.react("ops-solve-status", [{
        type: "bar",
        x: sks,
        y: sks.map(k => sc[k]),
        marker: { color: sks.map(k => STATUS_COLOR[k] || "#8b949e") },
      }], baseLayout({
        annotations: panelLabel("STATUS MIX", "ticks"),
      }), PLOT_CONFIG);
      window.eoChart.registerPlot("ops-solve-status");
    } else {
      $("ops-solve-status").innerHTML = '<div class="muted">no solves in window</div>';
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

    // Per-register write success/error breakdown
    const writes = body.writes || [];
    if (writes.length === 0) {
      $("ops-modbus-writes").innerHTML = '<div class="muted">no writes in window</div>';
      return;
    }
    const byReg = {};
    writes.forEach(w => {
      const k = String(w.register);
      if (!byReg[k]) byReg[k] = { ok: 0, err: 0 };
      if (w.event === "modbus_write") byReg[k].ok = w.n;
      else byReg[k].err = w.n;
    });
    const regs = Object.keys(byReg).sort((a, b) => Number(a) - Number(b));
    Plotly.react("ops-modbus-writes", [
      { type: "bar", name: "ok",  x: regs, y: regs.map(r => byReg[r].ok),  marker: { color: "#3fb950" } },
      { type: "bar", name: "err", x: regs, y: regs.map(r => byReg[r].err), marker: { color: "#f85149" } },
    ], baseLayout({
      annotations: panelLabel("WRITES PER REGISTER", "count"),
      barmode: "stack",
      xaxis: axis({ type: "category" }),
      legend: { orientation: "h", y: -0.25, font: { size: 11, family: FONT_FAMILY } },
    }), PLOT_CONFIG);
    window.eoChart.registerPlot("ops-modbus-writes");
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

    if (name === "ops") {
      // First visit: do an immediate refresh and start the timers.
      refreshAll();
      startPollers();
    } else {
      stopPollers();
    }

    // Force every Plotly chart in the now-visible tab to re-fit. Plotly's
    // `responsive: true` only listens to window.resize, and a chart that
    // was display:none during a resize keeps its stale inline width
    // afterwards — pushing the page wider than the viewport. The 50ms
    // delay lets the browser apply the `hidden` flip before resize sees
    // the new layout box.
    setTimeout(() => window.eoChart.resizeAll(), 50);
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
      if (!window.eoChart.isNarrow()) return;
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
    // so dragmode + tightened margins flip cleanly without waiting for the
    // next 30/60s poll. Cheap — every refresh* function is a no-op when
    // its panel isn't on screen (uses Plotly.react's idempotent path).
    window.eoChart.onBreakpointChange(() => {
      if (opsState.activeTab === "ops") refreshAll();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
