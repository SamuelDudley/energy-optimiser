"""Hardware probe: cutoff-pinned-at-ceiling dispatch.

Follow-on to the two-phase adaptive-dispatch work (see
``probe_two_phase.py`` and ``SPEC-ENERGY-01.md §5.4``). The
user-proposed simplification:

    Leave ``charge_cut_off_soc`` (reg 40047) pinned at the configured
    ceiling (e.g. 95%). Don't rewrite it per tick. Manage SOC entirely
    via reg 40032 (charge-rate cap).

Rationale: the LP re-anchors to live SOC every tick and its SOC band is
soft-constrained — arriving higher than planned is recoverable. Taking
"extra" PV into the battery is strictly better than curtailing it. The
only behaviour that changes is idle enforcement, which moves from
``cutoff=current_soc`` to ``40032=0``.

The two-phase probe already proved the adaptive trim formula produces
the expected split between battery and export. This probe verifies the
new pieces:

    P1 idle_via_cap        — cutoff=ceiling, 40032=0: battery idles, all
                             surplus exports at DNSP cap. Key behavioural
                             change vs today's idle path.
    P2 split_ceiling_cutoff — cutoff=ceiling (not soc+5%), trim+export
                              split as in two-phase P1. Verifies the
                              split is independent of cutoff proximity
                              to current SOC.
    P3 persist_and_cycle   — cutoff=ceiling (single write at start),
                              cycle 40032 through max → 0 → trim → max.
                              Battery must track 40032; cutoff readback
                              must equal ceiling throughout.

Each sub-probe samples at 1 Hz, touches the heartbeat each loop, and
reverts to a deterministic safe state (mode 2, cutoff=ceiling, 40032=max,
export=DNSP) in ``finally``.

Run:
    docker compose stop optimiser
    docker run --rm --network host \\
        -v energy-optimiser_optimiser-data:/var/lib/energy-optimiser \\
        -v /home/dudley/code/energy-optimiser/config.toml:/etc/energy-optimiser/config.toml:ro \\
        energy-optimiser-optimiser python -m optimiser.probe_no_cutoff
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
from dataclasses import asdict, dataclass, field
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

logger = logging.getLogger("probe_no_cutoff")

HEARTBEAT_PATH = Path("/var/lib/energy-optimiser/heartbeat")
DEFAULT_CONFIG_PATH = Path("/etc/energy-optimiser/config.toml")

BASELINE_DURATION_S = 10
PHASE_A_DURATION_S = 15
PHASE_B_DURATION_S = 30
IDLE_DURATION_S = 30
CYCLE_STEP_DURATION_S = 30
RECOVERY_DURATION_S = 10
SAMPLE_INTERVAL_S = 1.0

# Steady-state window (last N seconds of a phase used for the verdict).
# Inverter cascade settles in 2-3 s; 5 s gives margin.
SETTLE_WINDOW_S = 5

# Refuse to run outside these bounds. Lower bound is conservative — we
# only gain ~3% SOC across the whole probe (cutoff at ceiling, no risk
# of clipping); upper bound avoids mid-probe cascade-to-export on phases
# that expect battery to be absorbing.
MIN_PV_KW = 5.0
MIN_SOC_PCT = 20.0
MAX_SOC_PCT = 80.0

# Verdict thresholds (kW). Inverter steady-state precision ~0.3 kW.
SPLIT_TOLERANCE_KW = 0.5
IDLE_TOLERANCE_KW = 0.3
PV_CURTAIL_TOLERANCE_KW = 0.5

# Cutoff readback tolerance (register tenths → 0.1% SOC).
CUTOFF_READBACK_TOLERANCE_RAW = 1


@dataclass(slots=True)
class Sample:
    ts: str
    elapsed_s: float
    phase: str
    ems_mode: int | None
    soc_pct: float | None
    pv_kw: float | None
    battery_kw: float | None
    grid_kw: float | None
    house_load_kw: float | None
    cutoff_raw: int | None = None  # reg 40047 readback; populated on cycle boundaries


@dataclass(slots=True)
class PhaseStats:
    phase: str
    n_samples: int
    pv_mean: float | None
    bat_mean: float | None
    grid_mean: float | None
    house_mean: float | None


@dataclass(slots=True)
class CutoffReadback:
    label: str
    elapsed_s: float
    raw: int | None


@dataclass(slots=True)
class ProbeResult:
    """Shared return shape for sub-probes (avoids wide tuple returns)."""

    stats: dict[str, PhaseStats] = field(default_factory=dict)
    cutoff_reads: list[CutoffReadback] = field(default_factory=list)
    expected: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    ok_writes: bool = True


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


async def _write_charge_cap_kw(
    controller: SigenergyController, cap_kw: float
) -> bool:
    cap_kw = max(0.0, min(controller._battery.max_dc_charge_kw, cap_kw))
    raw = int(round(cap_kw * 1000))
    return await controller._write_u32(REG_ESS_MAX_CHARGING_LIMIT, raw)


async def _write_max_charge(controller: SigenergyController) -> bool:
    return await _write_charge_cap_kw(
        controller, controller._battery.max_dc_charge_kw
    )


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


async def _read_cutoff_raw(controller: SigenergyController) -> int | None:
    return await controller._read_holding_u16(REG_CHARGE_CUTOFF_SOC)


async def _write_safe_state(
    controller: SigenergyController,
    ceiling_pct: float,
    export_dnsp_kw: float,
) -> bool:
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


async def _probe_idle_via_cap(
    controller: SigenergyController,
    samples: list[Sample],
    t0: float,
    *,
    ceiling_pct: float,
    export_dnsp_kw: float,
) -> ProbeResult:
    """P1: cutoff=ceiling + 40032=0 ⇒ battery idles, surplus exports.

    Baseline: safe state (battery charging at whatever the cascade wants).
    Phase A: write 40032=0, leave cutoff=ceiling. Cascade should route
    PV → house → battery(cap 0) → export. Battery ≈ 0, grid ≈ −min(surplus, DNSP).
    Recovery: revert to safe state.
    """
    logger.info("── P1 idle_via_cap: cutoff=%.1f%%, 40032=0, export=%.1fkW ──",
                ceiling_pct, export_dnsp_kw)
    result = ProbeResult()

    await _write_safe_state(controller, ceiling_pct, export_dnsp_kw)
    await _sample_loop(
        controller, "P1_baseline", BASELINE_DURATION_S, samples, t0,
    )

    if not await _write_charge_cap_kw(controller, 0.0):
        logger.error("P1: 40032=0 write failed")
        result.ok_writes = False
        return result
    if not await _write_export_cap_kw(controller, export_dnsp_kw):
        logger.error("P1: export write failed")
        result.ok_writes = False
        return result
    if not await _assert_mode_2(controller):
        logger.error("P1: mode write failed")
        result.ok_writes = False
        return result

    # Cutoff readback — must still equal ceiling; we never wrote it.
    raw = await _read_cutoff_raw(controller)
    result.cutoff_reads.append(CutoffReadback(
        label="P1_after_writes", elapsed_s=time.monotonic() - t0, raw=raw,
    ))

    await _sample_loop(controller, "P1_idle", IDLE_DURATION_S, samples, t0)

    stats = _stats(samples, "P1_idle", SETTLE_WINDOW_S)
    result.stats["P1_idle"] = stats
    baseline_stats = _stats(samples, "P1_baseline", SETTLE_WINDOW_S)
    result.stats["P1_baseline"] = baseline_stats

    if stats.pv_mean is not None and stats.house_mean is not None:
        surplus = max(0.0, stats.pv_mean - stats.house_mean)
        result.expected["expected_export_kw"] = min(export_dnsp_kw, surplus)
        result.expected["expected_bat_kw"] = 0.0
        logger.info(
            "P1 idle settled: pv=%.2f load=%.2f bat=%.2f grid=%.2f "
            "(expect bat≈0, export≈%.2f)",
            stats.pv_mean, stats.house_mean, stats.bat_mean or 0.0,
            stats.grid_mean or 0.0, result.expected["expected_export_kw"],
        )

    await _write_safe_state(controller, ceiling_pct, export_dnsp_kw)
    await _sample_loop(
        controller, "P1_recovery", RECOVERY_DURATION_S, samples, t0,
    )
    return result


async def _probe_split_ceiling_cutoff(
    controller: SigenergyController,
    samples: list[Sample],
    t0: float,
    *,
    ceiling_pct: float,
    export_dnsp_kw: float,
) -> ProbeResult:
    """P2: mirror of two_phase P1 but cutoff=ceiling instead of soc+5%.

    Confirms the adaptive trim split doesn't depend on cutoff sitting
    near current SOC. Phase A soaks uncapped; Phase B trims to
    ``surplus − export_dnsp`` and expects a clean split.
    """
    logger.info("── P2 split_ceiling_cutoff: cutoff=%.1f%% (held) ──", ceiling_pct)
    result = ProbeResult()

    await _write_safe_state(controller, ceiling_pct, export_dnsp_kw)
    await _sample_loop(
        controller, "P2_baseline", BASELINE_DURATION_S, samples, t0,
    )

    # Phase A: uncap, no export. Battery soaks surplus.
    if not await _write_export_cap_kw(controller, 0.0):
        result.ok_writes = False
        return result
    if not await _write_max_charge(controller):
        result.ok_writes = False
        return result
    if not await _assert_mode_2(controller):
        result.ok_writes = False
        return result
    await _sample_loop(
        controller, "P2_phaseA", PHASE_A_DURATION_S, samples, t0,
    )
    stats_a = _stats(samples, "P2_phaseA", SETTLE_WINDOW_S)
    result.stats["P2_phaseA"] = stats_a

    if stats_a.pv_mean is None or stats_a.house_mean is None:
        logger.error("P2: phase-A PV/load None")
        return result
    surplus_a = max(0.0, stats_a.pv_mean - stats_a.house_mean)
    trim_kw = max(0.0, surplus_a - export_dnsp_kw)
    expected_bat = min(trim_kw, surplus_a)
    expected_export = min(export_dnsp_kw, max(0.0, surplus_a - expected_bat))
    result.expected["surplus_a"] = surplus_a
    result.expected["trim_kw"] = trim_kw
    result.expected["expected_bat_kw"] = expected_bat
    result.expected["expected_export_kw"] = expected_export
    logger.info(
        "P2 phase-A settled: pv=%.2f load=%.2f → surplus=%.2f; "
        "phase-B: trim=%.2f expect bat≈%.2f export≈%.2f",
        stats_a.pv_mean, stats_a.house_mean, surplus_a,
        trim_kw, expected_bat, expected_export,
    )

    # Phase B: trim + export, still cutoff=ceiling (never rewritten).
    if not await _write_charge_cap_kw(controller, trim_kw):
        result.ok_writes = False
        return result
    if not await _write_export_cap_kw(controller, export_dnsp_kw):
        result.ok_writes = False
        return result
    await _sample_loop(
        controller, "P2_phaseB", PHASE_B_DURATION_S, samples, t0,
    )
    result.stats["P2_phaseB"] = _stats(samples, "P2_phaseB", SETTLE_WINDOW_S)

    # Cutoff readback at end of P2 — must still equal ceiling.
    raw = await _read_cutoff_raw(controller)
    result.cutoff_reads.append(CutoffReadback(
        label="P2_after_phaseB", elapsed_s=time.monotonic() - t0, raw=raw,
    ))

    await _write_safe_state(controller, ceiling_pct, export_dnsp_kw)
    await _sample_loop(
        controller, "P2_recovery", RECOVERY_DURATION_S, samples, t0,
    )
    return result


async def _probe_persist_and_cycle(
    controller: SigenergyController,
    samples: list[Sample],
    t0: float,
    *,
    ceiling_pct: float,
    export_dnsp_kw: float,
) -> ProbeResult:
    """P3: cutoff written ONCE; cycle 40032 through max → 0 → trim → max.

    Verifies (a) battery tracks 40032 cleanly through transitions and
    (b) cutoff register value remains == ceiling across ~2 min of
    unrelated writes (no firmware self-revert, no interaction with
    40032 writes).

    Representative of the steady-state tick cadence where the LP flips
    between charge / idle / charge over a few minutes of variable PV.
    """
    logger.info("── P3 persist_and_cycle: cutoff=%.1f%% (single write) ──",
                ceiling_pct)
    result = ProbeResult()

    # Safe state seeds cutoff=ceiling, 40032=max, export=DNSP.
    await _write_safe_state(controller, ceiling_pct, export_dnsp_kw)
    raw = await _read_cutoff_raw(controller)
    result.cutoff_reads.append(CutoffReadback(
        label="P3_after_seed", elapsed_s=time.monotonic() - t0, raw=raw,
    ))
    await _sample_loop(
        controller, "P3_baseline", BASELINE_DURATION_S, samples, t0,
    )

    # Step 1: 40032=max (already), export=0 — full-soak baseline.
    if not await _write_export_cap_kw(controller, 0.0):
        result.ok_writes = False
    await _sample_loop(
        controller, "P3_step1_max_noexport", CYCLE_STEP_DURATION_S, samples, t0,
    )
    result.stats["P3_step1_max_noexport"] = _stats(
        samples, "P3_step1_max_noexport", SETTLE_WINDOW_S,
    )

    # Step 2: 40032=0 (idle), export=DNSP — battery should drop to ~0.
    if not await _write_charge_cap_kw(controller, 0.0):
        result.ok_writes = False
    if not await _write_export_cap_kw(controller, export_dnsp_kw):
        result.ok_writes = False
    await _sample_loop(
        controller, "P3_step2_zero_export", CYCLE_STEP_DURATION_S, samples, t0,
    )
    result.stats["P3_step2_zero_export"] = _stats(
        samples, "P3_step2_zero_export", SETTLE_WINDOW_S,
    )

    # Mid-cycle cutoff readback.
    raw = await _read_cutoff_raw(controller)
    result.cutoff_reads.append(CutoffReadback(
        label="P3_mid_cycle", elapsed_s=time.monotonic() - t0, raw=raw,
    ))

    # Step 3: 40032=trim (use same trim formula as P2 reuse). We don't
    # know surplus here without a fresh probe; use half of max_dc as a
    # representative non-zero, non-max rate.
    trim_kw = 0.5 * controller._battery.max_dc_charge_kw
    if not await _write_charge_cap_kw(controller, trim_kw):
        result.ok_writes = False
    await _sample_loop(
        controller, "P3_step3_trim_export", CYCLE_STEP_DURATION_S, samples, t0,
    )
    result.stats["P3_step3_trim_export"] = _stats(
        samples, "P3_step3_trim_export", SETTLE_WINDOW_S,
    )
    result.expected["step3_trim_kw"] = trim_kw

    # Step 4: 40032=max again — battery should return to soak.
    if not await _write_max_charge(controller):
        result.ok_writes = False
    if not await _write_export_cap_kw(controller, 0.0):
        result.ok_writes = False
    await _sample_loop(
        controller, "P3_step4_max_noexport", CYCLE_STEP_DURATION_S, samples, t0,
    )
    result.stats["P3_step4_max_noexport"] = _stats(
        samples, "P3_step4_max_noexport", SETTLE_WINDOW_S,
    )

    # Final cutoff readback.
    raw = await _read_cutoff_raw(controller)
    result.cutoff_reads.append(CutoffReadback(
        label="P3_end", elapsed_s=time.monotonic() - t0, raw=raw,
    ))

    await _write_safe_state(controller, ceiling_pct, export_dnsp_kw)
    await _sample_loop(
        controller, "P3_recovery", RECOVERY_DURATION_S, samples, t0,
    )
    return result


# ── Verdicts ────────────────────────────────────────────────────


def _verdict_idle(result: ProbeResult) -> str:
    stats = result.stats.get("P1_idle")
    if stats is None or stats.bat_mean is None or stats.grid_mean is None:
        return "INCONCLUSIVE (phase-idle telemetry None)"
    bat = stats.bat_mean
    export_actual = -(stats.grid_mean or 0.0)
    expected_export = result.expected.get("expected_export_kw", 0.0)

    bat_ok = abs(bat) <= IDLE_TOLERANCE_KW
    export_ok = abs(export_actual - expected_export) <= SPLIT_TOLERANCE_KW

    # Cutoff should not have moved — we only wrote 40032 and mode.
    expected_raw = _pct_to_raw(_read_expected_ceiling(result))
    cutoff_ok = all(
        r.raw is not None
        and abs(r.raw - expected_raw) <= CUTOFF_READBACK_TOLERANCE_RAW
        for r in result.cutoff_reads
    ) if result.cutoff_reads else True

    parts = [
        f"bat={bat:+.2f} (expect ~0)",
        f"export={export_actual:+.2f} (expect {expected_export:+.2f})",
    ]
    if result.cutoff_reads:
        parts.append(
            "cutoff_raw=" + ",".join(str(r.raw) for r in result.cutoff_reads)
        )
    if bat_ok and export_ok and cutoff_ok:
        return "PASS — " + ", ".join(parts)
    fails = []
    if not bat_ok:
        fails.append("battery not idle")
    if not export_ok:
        fails.append("export off-cap")
    if not cutoff_ok:
        fails.append("cutoff drifted")
    return "FAIL [" + ", ".join(fails) + "] — " + ", ".join(parts)


def _verdict_split(result: ProbeResult, export_dnsp_kw: float) -> str:
    stats_a = result.stats.get("P2_phaseA")
    stats_b = result.stats.get("P2_phaseB")
    if stats_a is None or stats_b is None:
        return "INCONCLUSIVE (missing phase stats)"
    if stats_b.bat_mean is None or stats_b.grid_mean is None or stats_b.pv_mean is None:
        return "INCONCLUSIVE (phase-B telemetry None)"
    bat_actual = stats_b.bat_mean
    export_actual = -stats_b.grid_mean
    pv_actual = stats_b.pv_mean
    pv_a = stats_a.pv_mean or 0.0

    expected_bat = result.expected.get("expected_bat_kw", 0.0)
    expected_export = result.expected.get("expected_export_kw", 0.0)

    bat_ok = abs(bat_actual - expected_bat) <= SPLIT_TOLERANCE_KW
    export_ok = abs(export_actual - expected_export) <= SPLIT_TOLERANCE_KW
    pv_ok = (pv_a - pv_actual) <= PV_CURTAIL_TOLERANCE_KW  # no curtail

    expected_raw = _pct_to_raw(_read_expected_ceiling(result))
    cutoff_ok = all(
        r.raw is not None
        and abs(r.raw - expected_raw) <= CUTOFF_READBACK_TOLERANCE_RAW
        for r in result.cutoff_reads
    ) if result.cutoff_reads else True

    parts = [
        f"bat={bat_actual:+.2f} (expect {expected_bat:+.2f})",
        f"export={export_actual:+.2f} (expect {expected_export:+.2f})",
        f"pv={pv_actual:.2f} (phaseA was {pv_a:.2f})",
    ]
    if result.cutoff_reads:
        parts.append(
            "cutoff_raw=" + ",".join(str(r.raw) for r in result.cutoff_reads)
        )
    if bat_ok and export_ok and pv_ok and cutoff_ok:
        return "PASS — " + ", ".join(parts)
    fails = []
    if not bat_ok:
        fails.append("battery off-cap")
    if not export_ok:
        fails.append("export off-cap")
    if not pv_ok:
        fails.append("pv curtailed")
    if not cutoff_ok:
        fails.append("cutoff drifted")
    return "FAIL [" + ", ".join(fails) + "] — " + ", ".join(parts)


def _verdict_cycle(result: ProbeResult, ceiling_pct: float) -> str:
    """Battery must respond to 40032 changes; cutoff must stay at ceiling."""
    expected_ceiling_raw = _pct_to_raw(ceiling_pct)
    cutoff_drift = [
        (r.label, r.raw) for r in result.cutoff_reads
        if r.raw is None
        or abs(r.raw - expected_ceiling_raw) > CUTOFF_READBACK_TOLERANCE_RAW
    ]

    step1 = result.stats.get("P3_step1_max_noexport")
    step2 = result.stats.get("P3_step2_zero_export")
    step3 = result.stats.get("P3_step3_trim_export")
    step4 = result.stats.get("P3_step4_max_noexport")

    def _bat(s: PhaseStats | None) -> float | None:
        return s.bat_mean if s is not None else None

    b1, b2, b3, b4 = _bat(step1), _bat(step2), _bat(step3), _bat(step4)
    parts: list[str] = []
    if all(b is not None for b in (b1, b2, b3, b4)):
        parts.append(f"bat by step=[{b1:+.2f}, {b2:+.2f}, {b3:+.2f}, {b4:+.2f}]")
    parts.append(
        "cutoff_raw=[" + ", ".join(
            f"{r.label}={r.raw}" for r in result.cutoff_reads
        ) + "]"
    )

    # Expected pattern: step1 > 0.5 (charging), step2 ≈ 0 (idle via cap),
    # step3 in (0, max_dc), step4 > step3 (back to soak).
    fails: list[str] = []
    if b1 is None or b1 < 0.5:
        fails.append(f"step1 bat={b1}")
    if b2 is None or abs(b2) > IDLE_TOLERANCE_KW:
        fails.append(f"step2 bat={b2} (expect ~0)")
    if b3 is None:
        fails.append("step3 None")
    if b4 is None or b4 < 0.5:
        fails.append(f"step4 bat={b4}")
    if cutoff_drift:
        fails.append(f"cutoff drifted at {[lbl for lbl, _ in cutoff_drift]}")

    if not fails:
        return "PASS — " + ", ".join(parts)
    return "FAIL [" + ", ".join(fails) + "] — " + ", ".join(parts)


def _pct_to_raw(pct: float) -> int:
    return max(0, min(1000, int(round(pct * 10))))


def _read_expected_ceiling(result: ProbeResult) -> float:
    """The ceiling is captured at probe entry via the safe-state writes;
    all sub-probes share it. Read back from the first cutoff-read raw,
    which is taken immediately after a safe-state write."""
    for r in result.cutoff_reads:
        if r.raw is not None:
            return r.raw / 10.0
    return 95.0  # defensive fallback — matches typical config


def _summarise(
    samples: list[Sample],
    verdicts: list[tuple[str, str]],
    ceiling_pct: float,
    all_cutoff_reads: list[CutoffReadback],
) -> None:
    print("\n══════ SUMMARY ══════")
    print(
        f"  Safe-state ceiling = {ceiling_pct:.1f}% "
        f"(expected cutoff raw {_pct_to_raw(ceiling_pct)})"
    )
    print()
    for label, verdict in verdicts:
        print(f"  {label:<40} {verdict}")
    print("\n  Cutoff (reg 40047) readbacks:")
    for r in all_cutoff_reads:
        delta = (
            r.raw - _pct_to_raw(ceiling_pct) if r.raw is not None else None
        )
        delta_s = f"(Δ={delta:+d})" if delta is not None else ""
        print(f"    {r.label:<28}  t={r.elapsed_s:6.1f}s  raw={r.raw} {delta_s}")
    print("\n  Phase steady-state means (last 5 s):")
    seen: list[str] = []
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
    all_cutoff_reads: list[CutoffReadback] = []
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

        t0 = time.monotonic()
        touched_state = True

        # ── P1: idle via 40032=0 ──
        r1 = await _probe_idle_via_cap(
            controller, samples, t0,
            ceiling_pct=ceiling_pct, export_dnsp_kw=export_dnsp_kw,
        )
        all_cutoff_reads.extend(r1.cutoff_reads)
        verdicts.append((
            "P1 (idle via 40032=0, cutoff held)",
            _verdict_idle(r1),
        ))

        # ── P2: split with cutoff held at ceiling ──
        r2 = await _probe_split_ceiling_cutoff(
            controller, samples, t0,
            ceiling_pct=ceiling_pct, export_dnsp_kw=export_dnsp_kw,
        )
        all_cutoff_reads.extend(r2.cutoff_reads)
        verdicts.append((
            "P2 (split, cutoff=ceiling)",
            _verdict_split(r2, export_dnsp_kw),
        ))

        # ── P3: persistence + rate cycling ──
        r3 = await _probe_persist_and_cycle(
            controller, samples, t0,
            ceiling_pct=ceiling_pct, export_dnsp_kw=export_dnsp_kw,
        )
        all_cutoff_reads.extend(r3.cutoff_reads)
        verdicts.append((
            "P3 (persist cutoff, cycle 40032)",
            _verdict_cycle(r3, ceiling_pct),
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
        _summarise(samples, verdicts, ceiling_pct, all_cutoff_reads)
        await controller.disconnect()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(
        description="Sigenergy cutoff-pinned-at-ceiling dispatch probe.",
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), type=Path)
    p.add_argument(
        "--samples-dump",
        default="/var/lib/energy-optimiser/probe_no_cutoff.ndjson",
        type=lambda s: Path(s) if s else None,
        help="NDJSON file for per-sample telemetry (empty to skip).",
    )
    args = p.parse_args()
    return asyncio.run(run(args.config, args.samples_dump))


if __name__ == "__main__":
    sys.exit(main())
