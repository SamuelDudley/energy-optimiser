# Sigenergy EMS Mode Behaviour — Hardware-Verified Reference

Empirical reference for what each `RemoteEMSControlMode` (register 40031)
actually does on our installed hardware. Written after three controlled
probes on 2026-04-23 that contradicted earlier assumptions baked into
`src/optimiser/lp/dispatch.py` and `SPEC-ENERGY-01.md §7.3`.

**Status:** authoritative for the installed hardware (Sigenergy inverter
at 192.168.2.220, 40 kWh battery, 13 kW PV array). Firmware version not
recorded at the probe timestamp — if firmware is updated, re-run the
probes before trusting this document.

**Mode 4 retired from the dispatch path 2026-04-24** (commit `acea3f5`,
the §3.3 cutoff dispatch). Mode 4's grid-draw hazard documented below
was the rationale; PV-dominant charge now uses mode 2 with the adaptive
trim on register 40032 (see SPEC-ENERGY-01.md §5.4). The mode 4 sections
in this document are retained as the empirical record that drove the
decision. Mode 4 stays in `RemoteEMSControlMode` for historical-snapshot
replay only.

**Charge-cutoff register (40047) retired from the tick path 2026-04-25**
(commit `1f363a7`). It's now pinned at `soc_ceiling_pct` by
`assert_battery_soc_limits` at startup and never rewritten per tick;
40032 alone governs charge rate. Validated by `probe_no_cutoff.py`.

**Probe scripts:** `src/optimiser/probe_mode4.py`, `probe_mode5.py`,
`probe_mode6.py`, `probe_two_phase.py` (mode-2 adaptive trim),
`probe_no_cutoff.py` (cutoff-pinned-at-ceiling). Raw samples dumped
under `/var/lib/energy-optimiser/probe_*.ndjson`.

---

## Why this document exists

Before the probes, dispatch selected:

- Mode 4 (CHARGE_PV_FIRST) with `cap = LP's intended kW` for any positive
  battery charge. **Assumed** the cap was an upper bound and that mode 4
  would never pull from grid if PV was sufficient.
- Mode 6 (DISCHARGE_ESS_FIRST) with `cap = max_discharge_kw` for any
  negative battery intent. **Assumed** mode 6 was the universal
  discharge answer regardless of PV availability.
- Mode 5 (DISCHARGE_PV_FIRST) was dismissed in a code comment as "lets
  the inverter skip battery discharge entirely if PV happens to cover
  house load, which contradicts the LP's intent when it asks to
  discharge."

All three assumptions turned out to be wrong or misleading in ways that
affect the money the system makes or loses. Write them down so the next
refactor doesn't undo the correction.

---

## Register-level controls

| Reg | Name | Type | Active in | Notes |
|---|---|---|---|---|
| 40029 | `REMOTE_EMS_ENABLE` | U16 | always | 1 = operator (remote) mode, 0 = local |
| 40031 | `REMOTE_EMS_CONTROL_MODE` | U16 | when 40029=1 | the main strategy selector (see below) |
| 40032 | `ESS_MAX_CHARGING_LIMIT` | U32 gain=1000, kW | modes 3 & 4 only | **this is a target, not a ceiling** — mode 4 will pull from grid to hit it |
| 40034 | `ESS_MAX_DISCHARGING_LIMIT` | U32 gain=1000, kW | modes 5 & 6 only | target for mode 6; upper bound for mode 5 |
| 40038 | `GRID_EXPORT_LIMIT` | U32 gain=1000, kW | all modes | authoritative. 0 blocks *all* export including battery discharge |

**Critical reading of 40032 (mode 4 cap):** the name says "max
charging limit" but the probed behaviour says "charge setpoint" — if
we write 13 kW and PV only provides 9 kW surplus, the inverter draws
grid to fill the remaining 4 kW. We verified this on 2026-04-23 02:45 UTC.

---

## Probe methodology (common to all three)

Each probe followed the same envelope:

1. Stop the main optimiser container (releases the Modbus TCP socket).
2. Run the probe in a one-shot container using the same image, with the
   same data volume mount so heartbeat writes reach the watchdog.
3. Phase 1 — **baseline** (10 s): sample existing state at 1 Hz without
   writing anything. Confirms what the service had been commanding.
4. Phase 2 — **probe** (120 s): write target mode + caps, sample at 1 Hz.
5. Phase 3 — **recovery** (15 s): revert to mode 2 + export cap 5 kW,
   sample at 1 Hz to verify the revert took effect.
6. Probe touches `/var/lib/energy-optimiser/heartbeat` every second so
   the watchdog sidecar's 90 s stale timer does not fire. If the probe
   crashes, the `finally` block writes the safe state; if *that* fails,
   the watchdog is the last line of defence.

Fields captured per sample: `ts`, `phase`, `ems_mode`, `soc_pct`,
`pv_kw`, `battery_kw` (+ charge, − discharge), `grid_kw` (+ import,
− export), `house_load_kw`, plus all four MPPT string V/A pairs.

**Pre-flight gate:** probes abort unless PV ≥ 6 kW and SOC ∈ [25%, 85%].

---

## Mode 2 — MAXIMUM_SELF_CONSUMPTION

**Probed indirectly** via `probe_two_phase.py` (mode-2 cascade with
varied 40032 caps) and `probe_no_cutoff.py` (idle behaviour with
`40032 = 0` and cutoff held at ceiling). Cascade priority:

`PV → house → battery (up to 40032) → export (up to 40038) → MPPT curtail`

- **Register 40032 IS honoured** as a cap on the cascade's battery leg.
  Earlier docs claimed it was ignored — verified false on hardware
  2026-04-24. Setting `40032 = 0` cleanly idles the battery (probe
  P1: bat = -0.02 kW with surplus 5.0 kW, all routed to export).
- **Register 40047 (charge-cutoff SOC)** sets the upper SOC bound for
  the cascade's battery leg. Pinned at `soc_ceiling_pct` at startup
  and never rewritten in the tick path
  (`probe_no_cutoff.py` P3: 5 readbacks across 256 s of unrelated
  40032 writes all returned raw=950, no firmware drift).
- **Never grid-charges.**
- Export cap 40038 is respected.

**Three usage patterns in the dispatch:**

1. **Idle**: `40032 = 0`, `mode = 2`. Battery doesn't charge from PV;
   surplus exports up to DNSP. Discharge still allowed (40032 caps
   charge only — discharge floor is 40048).
2. **PV-dominant charge (adaptive trim)**: Phase A `40032 = max_dc`
   for 5 s + read surplus, Phase B `40032 = trim` so battery + export
   split rather than cascade-saturate. See SPEC-ENERGY-01.md §5.4.
3. **Fallback**: `40032 = max_dc, 40038 = 0` (or DNSP, depending on
   export-price sign). Battery soaks all PV; never grid-charges.

**Limitation (resolved by adaptive trim):** the cascade is
sequential, so without an explicit cap, surplus PV saturates the
battery before any export flows. The trim formula
`max(LP_rate, surplus − export_cap − headroom)` lets the LP plan and
execute mixed battery+export slots cleanly.

---

## Mode 3 — COMMAND_CHARGING_GRID_FIRST

**Not probed** (not needed for today's question; low-risk — the whole
point of the mode is to pull from grid). Documented semantics:

- Charges battery at the `40032` rate, preferring grid, falling back to
  PV if grid is restricted.
- Intended for cheap-overnight grid-charge windows.

**When to use:** LP explicitly plans grid-charging at a cost-justified
rate.

---

## Mode 4 — COMMAND_CHARGING_PV_FIRST

**Probed 2026-04-23 02:43 UTC. Dump: `probe_mode4.ndjson`.**

Command: `40031 = 4, 40032 = 13 000 (13 kW), 40038 = 5 000 (5 kW)`.
Conditions: SOC 77.5%, house load ≈ 0.17 kW, PV (un-curtailed) ≈ 9.3 kW.

| Phase | PV (kW) | Battery (kW) | Grid (kW) | Load (kW) | Energy balance |
|---|---|---|---|---|---|
| Baseline (mode 7 / LP charging) | 6.35 | +1.13 | −5.00 | 0.22 | 6.35 ≈ 0.22 + 1.13 + 5.00 ✓ |
| **Probe (mode 4, cap 13 kW)** | **9.30** | **+12.69** | **+3.36** (import) | 0.17 | 9.30 + 3.36 = 12.69 + 0.17 ✓ |
| Recovery (mode 2) | ≈5.2 | ≈0 | −5.00 | 0.20 | normal |

**Verdict:** the `40032` charge cap is a **target**, not a ceiling. When
we wrote 13 kW with only ~9 kW of PV surplus available, the inverter
imported 3.36 kW from grid to hit the commanded rate.

**Observed MPPT behaviour:** PV jumped from 6.35 kW → 9.30 kW the
instant the cap was lifted. The MPPT had been clipped in the baseline
state by the production service's 0.5 kW cap. The 3 kW "missing PV"
we had been worried about was indeed real and available all along.

**Implication for dispatch:** `cap = max_dc_charge_kw` is **not safe**
as a default. Any cap above live `(pv − load)` pulls grid. The usable
cap is whichever is smaller of (a) the LP's economic intent, (b) live
`pv_kw − house_load_kw − small_margin`.

**When to use:** when the LP explicitly wants to control the charge
rate — e.g. export-first strategies where we deliberately want to leave
export headroom. Cap must be set from live telemetry, not just from the
LP's forecast, to avoid grid draw.

---

## Mode 5 — COMMAND_DISCHARGING_PV_FIRST

**Probed 2026-04-23 02:36 UTC. Dump: `probe_mode5.ndjson`.**

Command: `40031 = 5, 40034 = 5 000 (5 kW), 40038 = 5 000 (5 kW)`.
Conditions: SOC 77.1%, load ≈ 0.19 kW, PV (un-curtailed baseline) 6.33 kW.

| Phase | PV (kW) | Battery (kW) | Grid (kW) | Load (kW) |
|---|---|---|---|---|
| Baseline (LP charging at 1.13 kW) | 6.33 | +1.13 | −5.01 | 0.19 |
| **Probe (mode 5, disc_cap 5 kW)** | **5.20** | **+0.02** (idle) | −5.00 | 0.19 |
| Recovery (mode 2) | 5.16 | −0.01 | −5.00 | 0.17 |

**Verdict:** mode 5 is a **strict source selector**: "output comes from
PV, only from PV." With PV > `load + export cap`, surplus PV has
nowhere to go (battery is not allowed to absorb, because mode 5 is
discharging-semantics), so MPPT curtails the difference.

The ~1.1 kW drop from baseline to probe PV was the MPPT backing off —
load and export held steady, battery stayed at zero, and no grid flow
appeared. Energy balance confirms the missing kW was simply not
generated.

**When to use:** discharge scenarios where PV is *less than* what's
needed for load + export. Mode 5 gracefully blends PV and battery:
- PV alone covers load → battery idles (zero wear, good!)
- PV covers part of load+export → battery tops up the shortfall

**When NOT to use:** when PV > load + export. Use mode 2 instead;
mode 5 will curtail the surplus instead of routing it to battery.

**Important correction:** the old dispatch.py comment claimed mode 5
"skips battery discharge if PV covers house load, contradicting LP
intent." This was a misreading — when the LP asks to discharge at peak
prices, the *financial* intent is "export X kWh to grid", not "cycle
the battery." If PV alone can do the exporting, mode 5's skip-the-
battery behaviour is a win (zero wear for the same revenue). The only
case the old comment was right about is the rare "drop SOC deliberately
so I can grid-charge cheap later" scenario.

---

## Mode 6 — COMMAND_DISCHARGING_ESS_FIRST

**Probed 2026-04-23 02:52 UTC. Dump: `probe_mode6.ndjson`.**

Command: `40031 = 6, 40034 = 5 000 (5 kW), 40038 = 5 000 (5 kW)`.
Conditions: SOC 78.3%, load ≈ 0.17 kW, PV (un-curtailed baseline)
6.31 kW.

| Phase | PV (kW) | Battery (kW) | Grid (kW) | Load (kW) |
|---|---|---|---|---|
| Baseline (LP charging at 1.13 kW) | 6.31 | +1.13 | −5.01 | 0.17 |
| **Probe (mode 6, disc_cap 5 kW)** | **0.00** | **−5.00** (full cap discharge) | −4.52 | 0.49 |
| Recovery (mode 2) | ≈5.15 | ≈0 | −5.00 | 0.17 |

**Verdict: pathological for PV-surplus regimes.** The inverter
**entirely shut down PV generation** to execute the discharge command.
All load and export was served from battery alone for the full 120 s
probe. We lost 100% of available PV for the duration.

(The load reading climbed to ~0.5 kW during the probe — likely a small
thermostat-driven device cycling on; unrelated to the mode semantics.)

**When to use:** **only when PV is already zero** (deep night, or
heavy cloud with PV < 0.2 kW). Any time PV is producing meaningfully,
mode 6 will waste it.

**Critical dispatch note:** the current production code writes mode 6
for *any* discharge command from the LP, including evening-peak
discharges when PV is still producing. This is wasteful — we pay battery
wear to replace PV that the inverter just turned off. Fix is to gate
the mode 5 vs 6 choice on live `pv_kw`:

```
if battery_kw < 0:
    mode = DISCHARGE_PV_FIRST if measured_pv_kw > 0.2 else DISCHARGE_ESS_FIRST
```

---

## Summary: mode selection by LP intent

| LP intent | PV status now | Correct mode | Cap register | Notes |
|---|---|---|---|---|
| Idle / battery-first "use all PV" | any | **2** | `40038` = DNSP or 0 | default, safest |
| Grid charge (cheap window) | any | 3 | `40032` = LP rate | unchanged |
| Export-first (rare, high FIT) | PV available | 4 | `40032` = `min(LP rate, live_pv − load − 0.5)` | live-bounded cap |
| Discharge | PV > 0.2 kW | **5** | `40034` = `max_discharge_kw` | replaces mode 6 default |
| Discharge | PV ≈ 0 kW | 6 | `40034` = `max_discharge_kw` | only safe mode-6 use |
| Force SOC down (rare) | any | 6 | `40034` as needed | explicit edge case |

---

## Correction — mode 4 is safe for export-first when capped correctly

After reviewing the probe results alongside the LP formulation, an earlier
reading of this document (that there is "no safe way to export PV first and
soak the rest into the battery") is **too strong**. It conflates a cap
bug with a mode bug. Mode 4 *can* express export-first safely; the real
fixes are elsewhere. Capturing the nuance so future-us doesn't miss it:

### The mode 4 grid-draw in the probe was a cap bug, not a mode bug

The probe wrote `cap = 13 kW` while live `(pv − load) ≈ 9 kW`. The
inverter imported 3.36 kW to close the gap. The mode 4 rule, restated:

```
grid_draw_kw = max(0, cap − (live_pv − house_load))
```

With `cap ≤ live_pv − house_load − small_margin`, mode 4 draws **zero**
grid. Any surplus PV above the cap routes to the `40038` export limit (up
to DNSP max), any remainder curtails. That is exactly the "export-first,
overflow to battery" behaviour we want — mode 4 is the correct mode for
it, provided the cap is bounded by live telemetry.

### Why the running LP picked a 0.5 kW cap anyway

Even with mode 4 working correctly, the LP was writing a charge cap that
left ~3 kW of PV curtailed. Two root causes, both in the LP layer:

1. **Tied `grid_export[0]` non-anticipativity.** The stochastic LP
   currently enforces `grid_export[0]` equal across all three PV
   scenarios (P10 / P50 / P90). Because the slot-0 energy balance is
   `pv − load − battery_charge − export = 0`, tying export also ties
   `net_battery[0]`. The tied value is pinned by the worst-case (P10) PV
   — which in our current forecast was ~0.6 kW of surplus. Result: cap
   of 0.5 kW even though actual PV was producing 3 kW more.
2. **Flat L0 load profile.** The profiler hasn't accumulated enough
   historical data to move off the default flat 2.0 kW per-slot
   assumption. Overstating load in the scenarios depresses every
   scenario's feasible charge rate.

Fixing the first (untie `grid_export[0]`, keep the tie on `battery[0]`)
is the structural change. The tied `battery[0]` then climbs to
`P10_PV − load` (using whichever PV is actually lowest in slot 0), each
scenario picks its own export level for its own PV outcome, and only
the sub-P10 edge case briefly grid-draws — which is a correct hedge,
not a bug.

### Revised "safe export-first" picture after the LP fix

| Scenario | Mode | Cap (40032) | Export (40038) | Battery | Grid | Curtail? |
|---|---|---|---|---|---|---|
| Live PV ≥ LP's expected surplus | 4 | LP's intent (≤ `live_pv − load`) | DNSP max | charges at cap | 0 | 0 |
| Live PV < LP's expected surplus | 4 | LP's intent (> `live_pv − load`) | DNSP max | charges at `live_pv − load` + brief grid | brief import | 0 |
| Live PV > LP's cap + export | 4 | LP's intent | DNSP max | charges at cap | 0 | overage only |

The last row is the case we were worried about throwing away free
exports today that won't be available later. With the LP fix, cap grows
to absorb what the export cap can't take, so curtailment only appears
once `cap + export ≥ pv − load` — i.e. we're physically at the
inverter's combined rate limit. That's the right place to give up.

### Operational hazards of mode 4 (even when capped correctly)

Even with a cap chosen from live telemetry, mode 4 has two structural
risks that mode 2 does not. Both need to be accounted for before mode 4
is used as a default export-first dispatch.

**Hazard 1 — mode 4 has no transient load-following or PV-tracking.**

The steady-state equation under mode 4 is:

```
grid_import_kw = max(0, (cap + load) − pv)
```

Two transient classes push the right-hand side positive and cause grid
import at retail:

- **PV droop** (cloud, shadow, soiling, MPPT tracker slew). `pv` drops
  below `cap + load`; the inverter holds the battery at `cap` and
  imports from grid to close the gap.
- **Load spike** (kettle, AC compressor starting, EV, oven). `load`
  climbs; same equation, same grid import. The "PV-first" in the mode
  name refers to the battery's charge source priority, not to load
  following. The controller must serve the house load by any means, and
  with battery pre-allocated to `cap`, grid is the only fallback.

Mode 2 is transient-safe by construction: load always comes off
PV/battery first and export is the residual. Mode 4 inverts that — the
battery charge rate is fixed and the residual absorbs the transients.

**Hazard 2 — live `pv_kw` under mode 4 is a post-curtailment reading,
not an available-PV ceiling.**

Under mode 4 with cap = `C`, the measured PV is:

```
live_pv = min(available_pv, load + C + export_cap)
```

If the right-hand side is saturated, the MPPT is clipping and the number
we read understates what the panels could produce. Setting the next cap
from `live_pv − load − margin` is therefore self-reinforcing: a
conservative start stays conservative, because the MPPT never gets to
reveal its ceiling.

`live_pv` equals true available PV only when (a) we're in a mode that
does not constrain it (mode 2 below its saturation point), or (b)
we know headroom exists because `load + C + export_cap > P90_forecast`.

Implications for using mode 4 safely as the export-first default:

1. Need either a Solcast-forecast-driven ceiling (with margin for
   forecast miss) or periodic open-cap probing to learn available PV.
2. Need a reaction loop faster than cloud transients (seconds) if grid
   import from PV droop is to be bounded — or accept the cost as the
   price of export-first dispatch.
3. Load-spike import is unavoidable in mode 4 without a live load-
   following override. Budget it, or mitigate by setting cap lower than
   `live_pv − load` by a transient-absorption margin.

### What does NOT change

- Mode 6 is still **only** safe when PV ≈ 0.
- Mode 5 is still the right choice for discharge with PV producing.
- The `40032` register is still a target, not a ceiling — callers must
  still bound the cap by live telemetry before writing.
- Mode 2 remains the safest default for "battery-first, overflow
  export" — the transients it absorbs for free are a hazard class mode
  4 exposes us to.

---

## What we still don't know

- **Mode 3 behaviour under PV surplus.** Not tested — low priority since
  mode 3 is only invoked during planned grid-charge windows when PV is
  expected to be low. If we ever want to grid-charge during the day
  (unlikely on this tariff), re-check.
- **Mode 4 behaviour as PV ramps down mid-slot.** The cap is a target.
  If PV drops from 9 kW → 5 kW while cap=8 kW is commanded, does the
  inverter immediately start pulling grid? We assume yes based on the
  probe, but transition timing wasn't measured.
- **Battery BMS dynamic charge rate.** At high SOC (>90%) the BMS may
  taper max charge rate below the 13 kW DC nameplate. Not directly
  measured on this hardware; relevant for mode 2 behaviour near the
  ceiling.
- **Mode 5 load-spike response.** If a 3 kW load transient fires while
  mode 5 is discharging from PV only, does the battery absorb the spike
  or does grid import appear? Mode 5 might be strict enough to only
  supply from PV; test before relying on it for peak-shaving.
- **Firmware version.** We should log this at next restart. If
  Sigenergy rolls firmware, any of the above could change.

---

## Reproducing the probes

Requirements:
- PV ≥ 6 kW (probe preflight aborts otherwise).
- SOC ∈ [25%, 85%] (preflight).
- Service must be stopped to release the Modbus TCP socket.

Run one probe:

```bash
docker compose build optimiser
docker compose stop optimiser
docker run --rm --network host \
    -v energy-optimiser_optimiser-data:/var/lib/energy-optimiser \
    -v /home/dudley/code/energy-optimiser/config.toml:/etc/energy-optimiser/config.toml:ro \
    energy-optimiser-optimiser \
    python -m optimiser.probe_mode5   # or probe_mode4 / probe_mode6
docker compose start optimiser
```

Total downtime ≈ 3 min. Heartbeat is refreshed every second while the
probe runs so the watchdog stays green. On any crash the `finally`
block writes mode 2 + export cap 5 kW back.

**Do not run with SOC < 25% or > 85%.** Mode 6 can pull 10 kWh from
battery in short order if anything goes wrong; floor/ceiling headroom
keeps the worst case recoverable.

Samples land in `/var/lib/energy-optimiser/probe_mode{4,5,6}.ndjson`
for offline analysis. Each line is one JSON sample with ts, phase,
ems_mode, SOC, PV, battery, grid, load, and MPPT string V/A.

---

## Revision history

| Date | Change |
|---|---|
| 2026-04-23 | Initial document; mode 4 / 5 / 6 probes executed. Mode 2 documented from observation. Modes 0/1/3/7 not probed. |
| 2026-04-23 | Added "Correction — mode 4 is safe for export-first when capped correctly". Walks back an overly-strong reading of the mode 4 probe. The grid-draw was a cap-sizing bug, not a mode bug; real fix is in the LP formulation (untie `grid_export[0]` non-anticipativity) and load profiler. |
| 2026-04-23 | Added "Operational hazards of mode 4" subsection: no transient load-following or PV-tracking (droop/spike → grid import), and live `pv_kw` under mode 4 is a post-curtailment reading, not an available-PV ceiling. |
