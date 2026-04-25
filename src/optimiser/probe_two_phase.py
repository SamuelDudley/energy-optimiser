"""Hardware probe: validate the proposed two-phase mode-2 dispatch.

Bug #2 (in-conversation, 2026-04-24): mode 2's cascade gives sequential
"battery first, then export" behaviour. With 40032 uncapped (the §3.3
fix for missing PV), the battery soaks every kW of PV until SOC hits
cutoff before any export flows — so we lose the daytime window where a
mixed split (battery + export) would be profitable.

The proposed fix (user's idea): adapt 40032 each tick. Phase A writes
40032 = max so we can MEASURE true PV potential. Phase B trims 40032 to
``max(0, surplus − export_cap)`` so the cascade naturally splits between
battery and export without curtailing.

This probe runs the two phases as discrete writes and verifies the
resulting steady-state matches the expected split. We do this BEFORE
wiring the production code so we know:

    1. With ``40032 = max`` and ``export = 0``, the battery does soak
       all surplus PV (already verified — sanity baseline).
    2. With ``40032 = surplus_A − DNSP_export`` and ``export = DNSP``,
       the cascade splits as predicted: battery near the trim cap,
       export near the DNSP cap, PV unchanged.
    3. Probe an "over-trim" case where trim < (surplus − export_cap)
       to confirm the inverter curtails PV (not e.g. pulls grid).

Each sub-probe runs baseline(10s) → phase_A(15s) → phase_B(30s) →
recovery(10s). Mode pinned at 2 throughout. Heartbeat refreshed each
sample. ``finally`` block reverts to a deterministic safe state
(mode 2, cutoff = ceiling, 40032 = max, export = DNSP).

Run:
    docker compose stop optimiser
    docker run --rm --network host \\
        -v energy-optimiser_optimiser-data:/var/lib/energy-optimiser \\
        -v /home/dudley/code/energy-optimiser/config.toml:/etc/energy-optimiser/config.toml:ro \\
        energy-optimiser-optimiser python -m optimiser.probe_two_phase
    docker compose start optimiser

Total runtime: ~4 min.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .clients.sigenergy import (
    REG_CHARGE_CUTOFF_SOC,
    REG_ESS_MAX_CHARGING_LIMIT,
    REG_GRID_EXPORT_LIMIT,
    REG_REMOTE_EMS_CONTROL_MODE,
    REG_REMOTE_EMS_ENABLE,
    SigenergyController,
)
from .config import load_config
from .time_utils import now_utc
from .types import RemoteEMSControlMode

logger = logging.getLogger("probe_two_phase")

HEARTBEAT_PATH = Path("/var/lib/energy-optimiser/heartbeat")
DEFAULT_CONFIG_PATH = Path("/etc/energy-optimiser/config.toml")

BASELINE_DURATION_S = 10
PHASE_A_DURATION_S = 15
PHASE_B_DURATION_S = 30
RECOVERY_DURATION_S = 10
SAMPLE_INTERVAL_S = 1.0

# Steady-state window (seconds at end of each phase used for the verdict).
# Inverter cascade settles in 2-3 s; 5 s gives margin and 5 samples to
# average over.
SETTLE_WINDOW_S = 5

# Refuse to run outside these bounds — too little PV and "split"
# behaviour can't even exist; too high SOC and cutoff = current+5 would
# clip against the physical ceiling mid-probe.
MIN_PV_KW = 5.0
MIN_SOC_PCT = 30.0
MAX_SOC_PCT = 80.0

# DNSP export cap to use during phase B. Match BatteryConfig.export_limit_kw
# (read from config so it stays in sync).

# Verdict thresholds (kW). Inverter steady-state precision around 0.3 kW
# in our prior probes; we allow ±0.5 kW slack on each leg.
SPLIT_TOLERANCE_KW = 0.5
PV_CURTAIL_TOLERANCE_KW = 0.5


@dataclass(slots=True)
class Sample:
    ts: str
    elapsed_s: float
    phase: str  # "p{N}_{baseline|phaseA|phaseB|recovery}"
    ems_mode: int | None
    soc_pct: float | None
    pv_kw: float | None
    battery_kw: float | None
    grid_kw: float | None
    house_load_kw: float | None


@dataclass(slots=True)
class PhaseStats:
    phase: str
    n_samples: int
    pv_mean: float | None
    bat_mean: float | None
    grid_mean: float | None
    house_mean: float | None


async def touch_heartbeat() -> None:
    try:
        HEARTBEAT_PATH.touch(exist_ok=True)
    except OSError as exc:
        logger.warning("heartbeat touch failed: %s", exc)


async def _sample(
    controller: SigenergyController, phase: str, elapsed: float
) -> Sample:
    state = await controller.read_state()
    return Sample(
        ts=now_utc().isoformat(),
        elapsed_s=round(elapsed, 2),
        phase=phase,
        ems_mode=state.ems_mode if state else None,
        soc_pct=state.soc_pct if state else None,
        pv_kw=state.pv_power_kw if state else None,
        battery_kw=state.battery_power_kw if state else None,
        grid_kw=state.grid_power_kw if state else None,
        house_load_kw=state.house_load_kw if state else None,
    )


async def _sample_loop(
    controller: SigenergyController,
    phase: str,
    duration_s: float,
    out: list[Sample],
    t0: float,
) -> None:
    end = time.monotonic() + duration_s
    while time.monotonic() < end:
        await touch_heartbeat()
        sample = await _sample(controller, phase, time.monotonic() - t0)
        out.append(sample)
        logger.info(
            "[%s %5.1fs] mode=%s soc=%s pv=%s bat=%s grid=%s load=%s",
            phase,
            sample.elapsed_s,
            sample.ems_mode,
            sample.soc_pct,
            sample.pv_kw,
            sample.battery_kw,
            sample.grid_kw,
            sample.house_load_kw,
        )
        await asyncio.sleep(max(0.0, SAMPLE_INTERVAL_S - 0.05))


async def _assert_mode_2(controller: SigenergyController) -> bool:
    return await controller._write_u16(
        REG_REMOTE_EMS_CONTROL_MODE,
        RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION.value,
    )


async def _write_max_charge(controller: SigenergyController) -> bool:
    """40032 = max_dc_charge_kw — uncap the battery DC leg."""
    raw = int(round(controller._battery.max_dc_charge_kw * 1000))
    return await controller._write_u32(REG_ESS_MAX_CHARGING_LIMIT, raw)


async def _write_charge_cap_kw(
    controller: SigenergyController, cap_kw: float
) -> bool:
    """40032 = trim cap (clamped to [0, max_dc_charge_kw])."""
    cap_kw = max(0.0, min(controller._battery.max_dc_charge_kw, cap_kw))
    raw = int(round(cap_kw * 1000))
    return await controller._write_u32(REG_ESS_MAX_CHARGING_LIMIT, raw)


async def _write_export_cap_kw(
    controller: SigenergyController, cap_kw: float
) -> bool:
    raw = max(0, int(round(cap_kw * 1000)))
    return await controller._write_u32(REG_GRID_EXPORT_LIMIT, raw)


async def _write_cutoff_pct(
    controller: SigenergyController, pct: float
) -> bool:
    raw = max(0, min(1000, int(round(pct * 10))))
    return await controller._write_u16(REG_CHARGE_CUTOFF_SOC, raw)


async def _write_safe_state(
    controller: SigenergyController,
    ceiling_pct: float,
    export_dnsp_kw: float,
) -> bool:
    """Deterministic safe state: mode 2, cutoff=ceiling, 40032=max,
    export=DNSP. Called between sub-probes and in the finally block."""
    logger.warning(
        "→ writing SAFE state: mode=2, cutoff=%.1f%%, 40032=max, export=%.1fkW",
        ceiling_pct, export_dnsp_kw,
    )
    ok = True
    ok &= await _assert_mode_2(controller)
    ok &= await _write_max_charge(controller)
    ok &= await _write_cutoff_pct(controller, ceiling_pct)
    ok &= await _write_export_cap_kw(controller, export_dnsp_kw)
    return ok


def _settle_window(
    samples: list[Sample], phase: str, window_s: float
) -> list[Sample]:
    """Last ``window_s`` of samples in ``phase`` — the steady-state slice."""
    in_phase = [s for s in samples if s.phase == phase]
    if not in_phase:
        return []
    end_t = max(s.elapsed_s for s in in_phase)
    return [s for s in in_phase if s.elapsed_s >= end_t - window_s]


def _mean(xs: list[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def _stats(samples: list[Sample], phase: str, window_s: float) -> PhaseStats:
    win = _settle_window(samples, phase, window_s)
    return PhaseStats(
        phase=phase,
        n_samples=len(win),
        pv_mean=_mean([s.pv_kw for s in win]),
        bat_mean=_mean([s.battery_kw for s in win]),
        grid_mean=_mean([s.grid_kw for s in win]),
        house_mean=_mean([s.house_load_kw for s in win]),
    )


# ── Sub-probes ──────────────────────────────────────────────────


async def _probe_split(
    controller: SigenergyController,
    samples: list[Sample],
    t0: float,
    *,
    label: str,
    cutoff_pct: float,
    export_dnsp_kw: float,
    trim_offset_kw: float,
    ceiling_pct: float,
) -> tuple[PhaseStats | None, PhaseStats | None, float | None]:
    """Generic split probe.

    Phase A (uncapped): mode 2, 40032=max, export=0, cutoff=arg.
        Battery should soak ~all surplus PV.
    Phase B (trimmed): 40032 = max(0, surplus_A − export_dnsp + trim_offset),
        export = export_dnsp_kw.
        Battery should equal trim cap; export should saturate.

    ``trim_offset_kw``:
        0   → "perfect" formula (battery + export = surplus, no curtail)
        −2  → "over-trim" (battery cap below ideal; export should still
              cap at DNSP, leftover PV should curtail — verifies the
              cascade prefers curtail over grid-import or weirdness)
        +2  → "under-trim" (battery cap higher than ideal; battery
              should saturate at surplus − export_dnsp limited by
              cascade, export still caps at DNSP)

    Returns (phaseA_stats, phaseB_stats, expected_bat_kw).
    """
    logger.info(
        "── %s: cutoff=%.1f%%  export_dnsp=%.1fkW  trim_offset=%+.1fkW ──",
        label, cutoff_pct, export_dnsp_kw, trim_offset_kw,
    )

    # Baseline: full safe state for 10 s
    await _write_safe_state(controller, ceiling_pct, export_dnsp_kw)
    # Override cutoff for this probe (safe-state set ceiling)
    await _write_cutoff_pct(controller, cutoff_pct)
    await _sample_loop(
        controller, f"{label}_baseline", BASELINE_DURATION_S, samples, t0,
    )

    # ── PHASE A: full sink, no export ──
    if not await _write_export_cap_kw(controller, 0.0):
        logger.error("%s: phase-A export write failed", label)
        return (None, None, None)
    if not await _write_max_charge(controller):
        logger.error("%s: phase-A 40032 write failed", label)
        return (None, None, None)
    if not await _write_cutoff_pct(controller, cutoff_pct):
        logger.error("%s: phase-A cutoff write failed", label)
        return (None, None, None)
    if not await _assert_mode_2(controller):
        logger.error("%s: phase-A mode write failed", label)
        return (None, None, None)
    await _sample_loop(
        controller, f"{label}_phaseA", PHASE_A_DURATION_S, samples, t0,
    )
    stats_a = _stats(samples, f"{label}_phaseA", SETTLE_WINDOW_S)

    if stats_a.pv_mean is None or stats_a.house_mean is None:
        logger.error("%s: phase-A returned None for PV or load — aborting", label)
        return (stats_a, None, None)
    surplus_a = max(0.0, stats_a.pv_mean - stats_a.house_mean)
    logger.info(
        "%s phase-A settled: pv=%.2f load=%.2f bat=%.2f grid=%.2f → surplus=%.2fkW",
        label, stats_a.pv_mean, stats_a.house_mean,
        stats_a.bat_mean or 0.0, stats_a.grid_mean or 0.0, surplus_a,
    )

    # ── PHASE B: trim + export ──
    trim_kw = max(0.0, surplus_a - export_dnsp_kw + trim_offset_kw)
    expected_bat = min(trim_kw, surplus_a)  # cascade can't exceed available
    expected_export = min(export_dnsp_kw, max(0.0, surplus_a - expected_bat))
    logger.info(
        "%s phase-B writing: trim=%.2fkW  export_cap=%.2fkW "
        "(expected: bat≈%.2f, export≈%.2f)",
        label, trim_kw, export_dnsp_kw, expected_bat, expected_export,
    )
    if not await _write_charge_cap_kw(controller, trim_kw):
        logger.error("%s: phase-B 40032 write failed", label)
        return (stats_a, None, expected_bat)
    if not await _write_export_cap_kw(controller, export_dnsp_kw):
        logger.error("%s: phase-B export write failed", label)
        return (stats_a, None, expected_bat)
    await _sample_loop(
        controller, f"{label}_phaseB", PHASE_B_DURATION_S, samples, t0,
    )
    stats_b = _stats(samples, f"{label}_phaseB", SETTLE_WINDOW_S)

    # Recovery
    await _write_safe_state(controller, ceiling_pct, export_dnsp_kw)
    await _sample_loop(
        controller, f"{label}_recovery", RECOVERY_DURATION_S, samples, t0,
    )

    return (stats_a, stats_b, expected_bat)


# ── Verdicts ────────────────────────────────────────────────────


def _verdict_split(
    stats_a: PhaseStats | None,
    stats_b: PhaseStats | None,
    expected_bat_kw: float | None,
    export_dnsp_kw: float,
    trim_offset_kw: float,
) -> str:
    if stats_a is None or stats_b is None or expected_bat_kw is None:
        return "INCONCLUSIVE (a phase failed to write or read)"
    if stats_b.bat_mean is None or stats_b.grid_mean is None or stats_b.pv_mean is None:
        return "INCONCLUSIVE (phase-B telemetry None)"
    bat_actual = stats_b.bat_mean
    export_actual = -stats_b.grid_mean  # grid_kw is +import, −export
    pv_actual = stats_b.pv_mean
    pv_a = stats_a.pv_mean or 0.0

    # Battery should follow trim cap (or surplus if trim > surplus)
    bat_ok = abs(bat_actual - expected_bat_kw) <= SPLIT_TOLERANCE_KW
    # Export should saturate near DNSP cap (unless over-trim leaves no room)
    surplus_a = (pv_a - (stats_a.house_mean or 0.0))
    expected_export = min(export_dnsp_kw, max(0.0, surplus_a - expected_bat_kw))
    export_ok = abs(export_actual - expected_export) <= SPLIT_TOLERANCE_KW
    # PV should NOT have dropped (no curtail) unless over-trim case
    pv_drop = pv_a - pv_actual
    if trim_offset_kw < 0:
        # Over-trim: curtail expected. Drop should equal |trim_offset|.
        expected_drop = abs(trim_offset_kw)
        pv_ok = abs(pv_drop - expected_drop) <= PV_CURTAIL_TOLERANCE_KW
    else:
        # No-trim or under-trim: PV should hold.
        pv_ok = pv_drop <= PV_CURTAIL_TOLERANCE_KW

    parts = [
        f"bat={bat_actual:+.2f} (expect {expected_bat_kw:+.2f})",
        f"export={export_actual:+.2f} (expect {expected_export:+.2f})",
        f"pv={pv_actual:.2f} (phaseA was {pv_a:.2f})",
    ]
    if bat_ok and export_ok and pv_ok:
        return "PASS — " + ", ".join(parts)
    fails = []
    if not bat_ok:
        fails.append("battery off-cap")
    if not export_ok:
        fails.append("export off-cap")
    if not pv_ok:
        fails.append(f"pv {'curtailed' if pv_drop > 0 else 'overproduced'}")
    return "FAIL [" + ", ".join(fails) + "] — " + ", ".join(parts)


def _summarise(
    samples: list[Sample],
    verdicts: list[tuple[str, str]],
    ceiling_pct: float,
) -> None:
    print("\n══════ SUMMARY ══════")
    print(f"  Safe-state ceiling = {ceiling_pct:.1f}%")
    print()
    for label, verdict in verdicts:
        print(f"  {label:<32} {verdict}")
    print("\n  Phase steady-state means (last 5 s):")
    seen = []
    for s in samples:
        if s.phase not in seen:
            seen.append(s.phase)
    for phase in seen:
        st = _stats(samples, phase, SETTLE_WINDOW_S)
        if st.n_samples == 0:
            continue
        pv = f"{st.pv_mean:.2f}" if st.pv_mean is not None else "—"
        bat = f"{st.bat_mean:+.2f}" if st.bat_mean is not None else "—"
        grid = f"{st.grid_mean:+.2f}" if st.grid_mean is not None else "—"
        load = f"{st.house_mean:.2f}" if st.house_mean is not None else "—"
        print(
            f"    {phase:<28}  pv={pv:>6} bat={bat:>6} grid={grid:>6} load={load:>5}"
        )


# ── Entry points ────────────────────────────────────────────────


async def run(
    config_path: Path,
    samples_dump: Path | None,
) -> int:
    config = load_config(config_path)
    controller = SigenergyController(config.sigenergy, config.battery)
    ceiling_pct = config.battery.soc_ceiling_pct
    export_dnsp_kw = config.battery.export_limit_kw

    logger.info(
        "Connecting to Sigenergy at %s:%d ...",
        config.sigenergy.host, config.sigenergy.port,
    )
    if not await controller.connect():
        logger.error("Modbus connect failed — is the service still holding the socket?")
        return 2

    samples: list[Sample] = []
    verdicts: list[tuple[str, str]] = []
    touched_state = False

    try:
        if not await controller._read_input_u16(REG_REMOTE_EMS_ENABLE):
            logger.warning("Remote EMS was disabled — enabling for the probe.")
            if not await controller.enable_remote_ems():
                logger.error("Could not enable Remote EMS — aborting.")
                return 3
        controller._remote_ems_enabled = True

        preflight = await controller.read_state()
        if preflight is None:
            logger.error("Could not read pre-flight state — aborting.")
            return 4
        pv = preflight.pv_power_kw or 0.0
        soc = preflight.soc_pct or 0.0
        load = preflight.house_load_kw or 0.0
        logger.info(
            "Pre-flight: PV=%.2fkW SOC=%.1f%% load=%.2fkW grid=%+.2fkW "
            "(safe window: PV≥%.1f, %.0f≤SOC≤%.0f)",
            pv, soc, load, preflight.grid_power_kw or 0.0,
            MIN_PV_KW, MIN_SOC_PCT, MAX_SOC_PCT,
        )
        if pv < MIN_PV_KW:
            logger.error("PV (%.2fkW) < %.1fkW — retry at higher irradiance.",
                         pv, MIN_PV_KW)
            return 5
        if not (MIN_SOC_PCT <= soc <= MAX_SOC_PCT):
            logger.error(
                "SOC (%.1f%%) outside [%.0f, %.0f]%% — retry later.",
                soc, MIN_SOC_PCT, MAX_SOC_PCT,
            )
            return 6

        # All probes use cutoff = soc_now + 5% so charge can flow during the probe
        # without hitting the ceiling and confusing the cascade. Capped to ceiling.
        probe_cutoff = min(ceiling_pct, soc + 5.0)
        logger.info(
            "Probe cutoff = %.1f%% (current SOC %.1f%% + 5%% headroom)",
            probe_cutoff, soc,
        )

        await _assert_mode_2(controller)
        t0 = time.monotonic()
        touched_state = True

        # ── Probe 1: ideal trim (no curtail expected) ──
        a1, b1, exp_bat_1 = await _probe_split(
            controller, samples, t0,
            label="P1_perfect_trim",
            cutoff_pct=probe_cutoff,
            export_dnsp_kw=export_dnsp_kw,
            trim_offset_kw=0.0,
            ceiling_pct=ceiling_pct,
        )
        verdicts.append((
            "P1 (perfect trim, expect split, no curtail)",
            _verdict_split(a1, b1, exp_bat_1, export_dnsp_kw, 0.0),
        ))

        # ── Probe 2: under-trim (more battery cap than needed) ──
        a2, b2, exp_bat_2 = await _probe_split(
            controller, samples, t0,
            label="P2_under_trim",
            cutoff_pct=probe_cutoff,
            export_dnsp_kw=export_dnsp_kw,
            trim_offset_kw=+2.0,
            ceiling_pct=ceiling_pct,
        )
        verdicts.append((
            "P2 (+2kW under-trim, battery cap > ideal)",
            _verdict_split(a2, b2, exp_bat_2, export_dnsp_kw, +2.0),
        ))

        # ── Probe 3: over-trim (battery cap below ideal — should curtail) ──
        a3, b3, exp_bat_3 = await _probe_split(
            controller, samples, t0,
            label="P3_over_trim",
            cutoff_pct=probe_cutoff,
            export_dnsp_kw=export_dnsp_kw,
            trim_offset_kw=-2.0,
            ceiling_pct=ceiling_pct,
        )
        verdicts.append((
            "P3 (−2kW over-trim, expect 2kW PV curtail)",
            _verdict_split(a3, b3, exp_bat_3, export_dnsp_kw, -2.0),
        ))

        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.warning("Interrupted — reverting to safe state.")
        return 130
    except Exception:
        logger.exception("Probe crashed — reverting to safe state.")
        return 1
    finally:
        if touched_state:
            try:
                await _write_safe_state(controller, ceiling_pct, export_dnsp_kw)
            except Exception:
                logger.exception("Safe-state revert FAILED — relying on watchdog.")
        if samples_dump and samples:
            samples_dump.write_text(
                "\n".join(json.dumps(asdict(s)) for s in samples) + "\n"
            )
            logger.info("Wrote %d samples to %s", len(samples), samples_dump)
        _summarise(samples, verdicts, ceiling_pct)
        await controller.disconnect()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(
        description="Sigenergy two-phase mode-2 dispatch probe.",
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), type=Path)
    p.add_argument(
        "--samples-dump",
        default="/var/lib/energy-optimiser/probe_two_phase.ndjson",
        type=lambda s: Path(s) if s else None,
        help="NDJSON file for per-sample telemetry (empty to skip).",
    )
    args = p.parse_args()
    return asyncio.run(run(args.config, args.samples_dump))


if __name__ == "__main__":
    sys.exit(main())
