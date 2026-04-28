"""Single-tick LP diagnostic — explains *why* the LP made the slot-0 decision
it did at one historical tick.

Usage:
    python -m optimiser.lp.diagnose_cli \\
        --snapshot '/var/lib/energy-optimiser/snapshots/2026-04-25.ndjson.gz' \\
        --timestamp '2026-04-25T08:35' \\
        --config /etc/energy-optimiser/config.toml

Optional counterfactual — force slot-0 to a different net battery kW and
report the per-scenario diff (this is how you find which scenario / future
slot is binding the LP's slot-0 choice via non-anticipativity):

    --force-bat-net -6.0     # force full discharge
    --force-bat-net  0.0     # force idle
    --force-bat-net  10.0    # force max grid charge

The forced solve sums weighted scenario costs the same way the real LP does;
the per-scenario diff lines name the slots where the LP traded discharge to
make room for the forced slot-0 choice. The biggest-Δ slot in the lowest-
weight scenario is usually the one driving the original decision (see
INVESTIGATION-evening-slot-skip.md for the worked example).

Read-only. Loads one snapshot, re-solves, never writes. Use the snapshot-
and-query pattern from DEPLOY.md if the snapshot path is inside the running
container's volume.
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

import pulp

from ..config import load_config
from ..replay import load_snapshots
from .constants import (
    EXPORT_TIE_BREAK_PENALTY_PER_KWH,
    PV_CURTAIL_PENALTY_PER_KWH,
    SOC_BOUND_PENALTY,
    WEAR_COST_PER_KWH,
)
from .formulation import _price_at, build_stochastic_lp
from .loads import build_lp_loads


def fmt(x: float) -> str:
    return f"{x:+.4f}"


def _solve(prob: pulp.LpProblem) -> int:
    """Run HiGHS in-process. Mirrors `lp.solver._highs()` — keep aligned if
    that ever changes (e.g. timeLimit tuning)."""
    return prob.solve(pulp.HiGHS(msg=False, timeLimit=30))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--snapshot",
        "-s",
        required=True,
        help="Path to one .ndjson or .ndjson.gz snapshot file",
    )
    p.add_argument(
        "--timestamp",
        "-t",
        required=True,
        help="Tick timestamp ISO prefix to diagnose, e.g. '2026-04-25T08:35'",
    )
    p.add_argument(
        "--config",
        "-c",
        required=True,
        help="LP config (battery, managed_loads). Use the live config to "
        "diagnose what the running service did.",
    )
    p.add_argument(
        "--override-soc",
        type=float,
        default=None,
        help="Override system_state.soc_pct before solving. For SOC-sensitivity "
        "tests; a fuller sweep is easier via replay_cli --override-soc.",
    )
    p.add_argument(
        "--force-bat-net",
        type=float,
        default=None,
        help="Add a constraint forcing slot-0 net battery kW = X "
        "(charge_grid + charge_pv − discharge). Triggers the per-scenario diff "
        "report that shows which future slot is binding the natural choice.",
    )
    p.add_argument(
        "--trajectory-slots",
        type=int,
        default=24,
        help="How many slots of base-scenario forward trajectory to print "
        "(default 24 = 2 hours).",
    )
    args = p.parse_args()

    cfg = load_config(args.config)
    snaps = [
        s for s in load_snapshots(args.snapshot)
        if s.timestamp.isoformat().startswith(args.timestamp)
    ]
    if not snaps:
        print(f"No snapshot matching '{args.timestamp}' in {args.snapshot}", file=sys.stderr)
        sys.exit(1)
    if len(snaps) > 1:
        print(
            f"WARN: {len(snaps)} snapshots match prefix; using first "
            f"({snaps[0].timestamp.isoformat()})",
            file=sys.stderr,
        )
    snap = snaps[0]

    if args.override_soc is not None:
        snap = dataclasses.replace(
            snap,
            system_state=dataclasses.replace(
                snap.system_state, soc_pct=args.override_soc
            ),
        )

    print(f"=== Tick: {snap.timestamp.isoformat()} ===")
    print(f"  SOC={snap.system_state.soc_pct}%  ems_mode={snap.system_state.ems_mode}")
    print(
        f"  measured: pv={snap.system_state.pv_power_kw}kW "
        f"house={snap.system_state.house_load_kw}kW "
        f"battery={snap.system_state.battery_power_kw}kW "
        f"grid={snap.system_state.grid_power_kw}kW"
    )
    print(
        f"  battery: cap={cfg.battery.capacity_kwh}kWh  max_dis={cfg.battery.max_discharge_kw}  "
        f"max_ac={cfg.battery.max_ac_charge_kw}  max_dc={cfg.battery.max_dc_charge_kw}  "
        f"export_lim={cfg.battery.export_limit_kw}"
    )
    print(
        f"  bounds: floor={cfg.battery.soc_floor_pct} backup={cfg.battery.backup_soc_pct} "
        f"cutoff={cfg.battery.discharge_cutoff_pct} ceiling={cfg.battery.soc_ceiling_pct}"
    )
    print(f"  prices: {len(snap.price_forecast)} intervals; pv_forecast: {len(snap.pv_forecast or [])} entries")

    lp_loads = build_lp_loads(cfg.managed_loads or [])
    prob, vars = build_stochastic_lp(
        state=snap.system_state,
        prices_planning=snap.price_forecast,
        pv_forecast=snap.pv_forecast,
        load_profile=snap.load_profile,
        managed_loads=snap.managed_loads,
        lp_loads=lp_loads,
        battery_config=cfg.battery,
    )
    status = _solve(prob)
    obj = pulp.value(prob.objective)
    print(f"\n=== Natural solve: status={pulp.LpStatus[status]}  obj={obj:.4f}c ===")
    print(f"  horizon: {len(vars.slots)} slots × {vars.slot_hours}h = {len(vars.slots) * vars.slot_hours:.1f}h")
    print(f"  base_scenario={vars.base_scenario}")

    base = vars.base
    slot_hours = vars.slot_hours
    price0 = _price_at(snap.price_forecast, base.slots[0])
    ip0 = price0.forecast_predicted if price0.forecast_predicted is not None else price0.import_per_kwh
    ep0 = (
        price0.export_forecast_predicted
        if price0.export_forecast_predicted is not None
        else price0.export_per_kwh
    )

    # Slot 0 across scenarios
    print("\n--- Slot 0 across scenarios (non-anti ties bat net only) ---")
    for name, sv in vars.scenarios.items():
        bcg = pulp.value(sv.bat_charge_grid[0])
        bcp = pulp.value(sv.bat_charge_pv[0])
        bd = pulp.value(sv.bat_discharge[0])
        gi = pulp.value(sv.grid_import[0])
        ge = pulp.value(sv.grid_export[0])
        net = bcg + bcp - bd
        soc_end = pulp.value(sv.soc_pct[0])
        print(
            f"  [{name} w={sv.weight}]  bcg={fmt(bcg)} bcp={fmt(bcp)} bd={fmt(bd)} "
            f"-> bat_net={fmt(net)}  ge={fmt(ge)} gi={fmt(gi)}  soc_end={fmt(soc_end)}"
        )

    # Objective decomposition for slot 0 in the BASE scenario only
    print("\n--- Slot-0 objective contribution (base scenario × weight) ---")
    bcg0 = pulp.value(base.bat_charge_grid[0])
    bcp0 = pulp.value(base.bat_charge_pv[0])
    bd0 = pulp.value(base.bat_discharge[0])
    gi0 = pulp.value(base.grid_import[0])
    ge0 = pulp.value(base.grid_export[0])
    pvc0 = pulp.value(base.pv_curtailed[0])
    soc_over0 = pulp.value(base.soc_over_ceiling[0])
    w = base.weight
    print(
        f"  ip(predicted-or-import) = {ip0:.4f}c/kWh, "
        f"ep(predicted-or-export) = {ep0:.4f}c/kWh, slot_hours = {slot_hours}"
    )
    print(f"    import_cost     = {w*gi0*ip0*slot_hours:+.4f}c")
    print(f"    export_revenue  = {-w*ge0*ep0*slot_hours:+.4f}c")
    print(f"    wear_charge     = {w*(bcg0+bcp0)*WEAR_COST_PER_KWH*slot_hours:+.4f}c")
    print(f"    wear_discharge  = {w*bd0*WEAR_COST_PER_KWH*slot_hours:+.4f}c")
    print(f"    pv_curtail_pen  = {w*pvc0*PV_CURTAIL_PENALTY_PER_KWH*slot_hours:+.4f}c")
    print(f"    soc_ceiling_pen = {w*SOC_BOUND_PENALTY*soc_over0:+.4f}c")
    if ep0 <= 0:
        print(
            f"    export_tie_pen  = {w*ge0*EXPORT_TIE_BREAK_PENALTY_PER_KWH*slot_hours:+.4f}c "
            f"(ep<=0, applies)"
        )
    else:
        print("    export_tie_pen  = 0  (ep>0, doesn't apply)")

    # Forward trajectory (base only)
    print(f"\n--- Forward trajectory (base scenario, first {args.trajectory_slots} slots) ---")
    print(f"  {'slot':>16}  {'bcg':>7} {'bcp':>7} {'bd':>7} {'ge':>7} {'gi':>7} {'soc':>7}  {'ep':>7}  {'ip':>7}")
    for t in range(min(args.trajectory_slots, len(base.slots))):
        s = base.slots[t]
        pr = _price_at(snap.price_forecast, s)
        ip_t = pr.forecast_predicted if pr.forecast_predicted is not None else pr.import_per_kwh
        ep_t = (
            pr.export_forecast_predicted
            if pr.export_forecast_predicted is not None
            else pr.export_per_kwh
        )
        print(
            f"  {s.isoformat()[:16]:>16}  "
            f"{pulp.value(base.bat_charge_grid[t]):7.3f} "
            f"{pulp.value(base.bat_charge_pv[t]):7.3f} "
            f"{pulp.value(base.bat_discharge[t]):7.3f} "
            f"{pulp.value(base.grid_export[t]):7.3f} "
            f"{pulp.value(base.grid_import[t]):7.3f} "
            f"{pulp.value(base.soc_pct[t]):7.3f}  "
            f"{ep_t:7.3f}  {ip_t:7.3f}"
        )

    # Horizon totals + terminal SOC
    n = len(base.slots)
    tot_bd = sum(pulp.value(base.bat_discharge[t]) for t in range(n)) * slot_hours
    tot_bcg = sum(pulp.value(base.bat_charge_grid[t]) for t in range(n)) * slot_hours
    tot_bcp = sum(pulp.value(base.bat_charge_pv[t]) for t in range(n)) * slot_hours
    tot_ge = sum(pulp.value(base.grid_export[t]) for t in range(n)) * slot_hours
    tot_gi = sum(pulp.value(base.grid_import[t]) for t in range(n)) * slot_hours
    tot_pvc = sum(pulp.value(base.pv_curtailed[t]) for t in range(n)) * slot_hours
    print("\n--- Horizon totals (base) ---")
    print(f"  discharge {tot_bd:.3f} kWh  charge_grid {tot_bcg:.3f}  charge_pv {tot_bcp:.3f}")
    print(f"  export    {tot_ge:.3f} kWh  import      {tot_gi:.3f}  pv_curtail {tot_pvc:.3f}")
    print("\n--- Terminal SOC (each scenario) ---")
    for name, sv in vars.scenarios.items():
        slack = pulp.value(sv.soc_terminal_slack) if sv.soc_terminal_slack is not None else 0.0
        print(f"  {name}: soc_end={pulp.value(sv.soc_pct[-1]):.3f}%  slack={slack:.4f}")

    # Optional counterfactual
    if args.force_bat_net is not None:
        print(f"\n=== Counterfactual: force slot-0 bat_net = {args.force_bat_net} ===")
        prob2, vars2 = build_stochastic_lp(
            state=snap.system_state,
            prices_planning=snap.price_forecast,
            pv_forecast=snap.pv_forecast,
            load_profile=snap.load_profile,
            managed_loads=snap.managed_loads,
            lp_loads=lp_loads,
            battery_config=cfg.battery,
        )
        b2 = vars2.base
        prob2 += (
            b2.bat_charge_grid[0] + b2.bat_charge_pv[0] - b2.bat_discharge[0]
            == args.force_bat_net,
            "force_slot0_net",
        )
        status2 = _solve(prob2)
        obj2 = pulp.value(prob2.objective)
        print(f"  status={pulp.LpStatus[status2]}  obj={obj2:.4f}c  Δ_vs_natural={obj2-obj:+.4f}c")
        print(f"  (positive Δ ⇒ natural was cheaper, i.e. the LP correctly preferred its choice)")

        # Per-scenario diff: shows where each scenario shifted activity to absorb
        # the forced slot-0 change. The slot with the largest abs(Δbd) in a low-
        # weight scenario is typically the one driving the natural decision.
        for sname in vars.scenarios:
            sv1 = vars.scenarios[sname]
            sv2 = vars2.scenarios[sname]
            print(f"\n  Scenario {sname} (w={sv1.weight}) — shifted slots (forced − natural):")
            shifted: list[tuple[int, float, float, float, float]] = []
            for t in range(n):
                d_bd = pulp.value(sv2.bat_discharge[t]) - pulp.value(sv1.bat_discharge[t])
                d_ge = pulp.value(sv2.grid_export[t]) - pulp.value(sv1.grid_export[t])
                d_bcp = pulp.value(sv2.bat_charge_pv[t]) - pulp.value(sv1.bat_charge_pv[t])
                d_bcg = pulp.value(sv2.bat_charge_grid[t]) - pulp.value(sv1.bat_charge_grid[t])
                if max(abs(d_bd), abs(d_ge), abs(d_bcp), abs(d_bcg)) > 0.01:
                    shifted.append((t, d_bd, d_ge, d_bcp, d_bcg))
            if not shifted:
                print("    (no shifts > 0.01 kW — scenario unaffected by forced slot 0)")
            else:
                # Sort by |Δbd|+|Δbcg|+|Δbcp| descending so the binding slots
                # surface first.
                shifted.sort(key=lambda r: -(abs(r[1]) + abs(r[3]) + abs(r[4])))
                for t, d_bd, d_ge, d_bcp, d_bcg in shifted[:8]:
                    s = base.slots[t]
                    pr = _price_at(snap.price_forecast, s)
                    ip_t = pr.forecast_predicted if pr.forecast_predicted is not None else pr.import_per_kwh
                    ep_t = (
                        pr.export_forecast_predicted
                        if pr.export_forecast_predicted is not None
                        else pr.export_per_kwh
                    )
                    print(
                        f"    {s.isoformat()[:16]}  "
                        f"Δbd={d_bd:+6.3f}  Δge={d_ge:+6.3f}  Δbcp={d_bcp:+6.3f}  Δbcg={d_bcg:+6.3f}  "
                        f"ep={ep_t:7.3f}c  ip={ip_t:7.3f}c"
                    )
            slack1 = pulp.value(sv1.soc_terminal_slack) if sv1.soc_terminal_slack is not None else 0.0
            slack2 = pulp.value(sv2.soc_terminal_slack) if sv2.soc_terminal_slack is not None else 0.0
            print(
                f"    SOC end:  natural={pulp.value(sv1.soc_pct[-1]):.3f}% (slack={slack1:.3f})  "
                f"forced={pulp.value(sv2.soc_pct[-1]):.3f}% (slack={slack2:.3f})"
            )


if __name__ == "__main__":
    main()
