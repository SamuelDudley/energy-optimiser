"""One-shot hardware probe: verify charge_cut_off_soc (reg 40047) behaviour.

Follow-up to probe_mode4 / probe_mode5, gating the §3.3 dispatch rewrite
(mode 4 → mode 2 + dynamic cutoff). Before we start writing reg 40047
every tick in production we need empirical answers to four questions:

    1. REWRITE FREQUENCY — Is reg 40047 safe to overwrite repeatedly?
       Some firmwares gate parameter writes on flash commits. If writes
       silently fail or alarms trip, we need a guard in the controller.

    2. CUTOFF BELOW CURRENT SOC — If SOC=55% and we write cutoff=50%,
       does the inverter idle (correct — "don't charge above"), try to
       discharge, or error? Expected: idle. We'd add a clamp otherwise.

    3. CUTOFF AT EXACTLY CURRENT SOC — Boundary-case stability.
       Expected: clean skip to the next priority (idle, no oscillation).

    4. SUPERSESSION — Startup writes 40047=ceiling via
       assert_battery_soc_limits(); under §3.3 the tick loop writes a
       smaller dynamic target over the top. Does the tick write win
       every time, or does some firmware "policy" layer push the
       ceiling back? If it reverts, §4.2's split (startup-only vs
       periodic) must land now.

Each sub-probe runs baseline(10s) → probe(N) → recovery(15s). Mode is
pinned at 2 (MAXIMUM_SELF_CONSUMPTION) throughout — only reg 40047
varies. The `finally` block writes a deterministic safe state (mode 2,
cutoff = soc_ceiling_pct × 10, export_cap = DNSP) regardless of outcome.
Heartbeat file is refreshed every sample so the watchdog sidecar
doesn't fire while the main service is stopped.

Run:
    docker compose stop optimiser
    docker run --rm --network host \\
        -v energy-optimiser_optimiser-data:/var/lib/energy-optimiser \\
        -v /home/dudley/code/energy-optimiser/config.toml:/etc/energy-optimiser/config.toml:ro \\
        energy-optimiser-optimiser python -m optimiser.probe_charge_cutoff
    docker compose start optimiser

Default total runtime: ~8 min. Pass/fail verdict for each sub-probe is
printed at the end; the NDJSON dump captures per-sample telemetry for
offline inspection.
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
    REG_BACKUP_SOC,
    REG_CHARGE_CUTOFF_SOC,
    REG_DISCHARGE_CUTOFF_SOC,
    REG_GRID_EXPORT_LIMIT,
    REG_INVERTER_ALARM1,
    REG_INVERTER_ALARM2,
    REG_INVERTER_ALARM3,
    REG_INVERTER_ALARM4,
    REG_INVERTER_ALARM5,
    REG_REMOTE_EMS_CONTROL_MODE,
    REG_REMOTE_EMS_ENABLE,
    SigenergyController,
)
from .config import load_config
from .time_utils import now_utc
from .types import RemoteEMSControlMode

logger = logging.getLogger("probe_charge_cutoff")

HEARTBEAT_PATH = Path("/var/lib/energy-optimiser/heartbeat")
DEFAULT_CONFIG_PATH = Path("/etc/energy-optimiser/config.toml")

# Per-probe durations (seconds). Tuned to fit ~8 min total while giving
# each sub-probe enough observation time to be meaningful. Override via
# CLI flag if you want a longer soak.
BASELINE_DURATION_S = 10
RECOVERY_DURATION_S = 15
SAMPLE_INTERVAL_S = 1.0

# Probe 1 (rewrite frequency): 12 writes × 10 s = 120 s. Enough to
# observe any latent firmware throttling without a 10-min soak.
P1_DURATION_S = 120
P1_WRITE_INTERVAL_S = 10.0

# Probe 2 (cutoff below SOC): 60 s dwell with inverter idle-check.
P2_DURATION_S = 60

# Probe 3 (cutoff at SOC): 60 s dwell, boundary stability check.
P3_DURATION_S = 60

# Probe 4 (supersession): 40 iterations × 5 s = 200 s. Enough repetitions
# to see firmware revert over a meaningful window if it happens.
P4_DURATION_S = 200
P4_WRITE_INTERVAL_S = 5.0

# Safety thresholds — refuse to run outside these bounds. The window is
# tighter than probe_mode5's because we're actively manipulating the
# charge cutoff; headroom above and below is required for the probes to
# be meaningful.
MIN_PV_KW = 2.0          # Probes 1/2/3 want some PV so "don't charge" is a real decision.
MIN_SOC_PCT = 55.0       # Probe 2 writes cutoff = (soc-5)%, so SOC must be ≥55.
MAX_SOC_PCT = 85.0       # Headroom above for probes that write cutoff = soc+2%.

# Verdict thresholds.
P1_MAX_ALARMS_OK = 0          # any new alarm bit → fail
P1_MAX_WRITE_FAILURE_FRAC = 0.01   # ≤1 in 100 is transient jitter, still pass
P1_READBACK_TOLERANCE_RAW = 1      # raw unit == 0.1%; allow ±1
P2_MAX_BATTERY_KW_ABS = 0.1        # inverter must idle, not discharge
P3_MAX_BATTERY_KW_ABS = 0.05       # tighter — boundary stability
P4_READBACK_TOLERANCE_RAW = 1


@dataclass(slots=True)
class Sample:
    """One telemetry snapshot. Richer than probe_mode5's because this
    probe cares about alarm bits + cutoff readback, not MPPT strings."""

    ts: str
    elapsed_s: float
    phase: str              # e.g. "p1_baseline", "p1_probe", "p2_probe", …
    ems_mode: int | None
    soc_pct: float | None
    pv_kw: float | None
    battery_kw: float | None
    grid_kw: float | None
    house_load_kw: float | None
    cutoff_raw: int | None  # last readback of reg 40047
    alarms: list[int | None]  # 5 alarm registers (None if read failed)


@dataclass(slots=True)
class WriteEvent:
    """Record every attempted write + readback pair for probe 1 + 4."""

    ts: str
    elapsed_s: float
    phase: str
    intended_raw: int
    write_ok: bool
    readback_raw: int | None
    drift_raw: int | None   # readback - intended (None if readback failed)


async def touch_heartbeat() -> None:
    try:
        HEARTBEAT_PATH.touch(exist_ok=True)
    except OSError as exc:
        logger.warning("heartbeat touch failed: %s", exc)


async def _read_cutoff(controller: SigenergyController) -> int | None:
    """Read reg 40047 raw value. Best-effort."""
    return await controller._read_holding_u16_best_effort(REG_CHARGE_CUTOFF_SOC)


async def _read_alarms(controller: SigenergyController) -> list[int | None]:
    """Read the five inverter alarm registers. Best-effort."""
    inv = controller._config.inverter_slave_id
    out: list[int | None] = []
    for reg in (
        REG_INVERTER_ALARM1,
        REG_INVERTER_ALARM2,
        REG_INVERTER_ALARM3,
        REG_INVERTER_ALARM4,
        REG_INVERTER_ALARM5,
    ):
        out.append(await controller._read_input_u16_best_effort(reg, slave_id=inv))
    return out


async def _sample(
    controller: SigenergyController, phase: str, elapsed: float
) -> Sample:
    state = await controller.read_state()
    cutoff_raw = await _read_cutoff(controller)
    alarms = await _read_alarms(controller)
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
        cutoff_raw=cutoff_raw,
        alarms=alarms,
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
            "[%s %5.1fs] mode=%s soc=%s pv=%s bat=%s grid=%s cutoff_raw=%s alarms=%s",
            phase,
            sample.elapsed_s,
            sample.ems_mode,
            sample.soc_pct,
            sample.pv_kw,
            sample.battery_kw,
            sample.grid_kw,
            sample.cutoff_raw,
            sample.alarms,
        )
        await asyncio.sleep(max(0.0, SAMPLE_INTERVAL_S - 0.05))


async def _write_cutoff_raw(
    controller: SigenergyController, raw: int
) -> bool:
    """Write reg 40047 directly. Returns the Modbus-write success flag."""
    clamped = max(0, min(1000, int(raw)))
    return await controller._write_u16(REG_CHARGE_CUTOFF_SOC, clamped)


async def _assert_mode_2(controller: SigenergyController) -> bool:
    """Ensure inverter is in mode 2 (MAXIMUM_SELF_CONSUMPTION)."""
    return await controller._write_u16(
        REG_REMOTE_EMS_CONTROL_MODE,
        RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION.value,
    )


async def _write_safe_state(
    controller: SigenergyController, ceiling_raw: int
) -> bool:
    """Deterministic safe state: mode 2, cutoff = ceiling, export = DNSP.
    Called between sub-probes and in the finally block."""
    logger.warning(
        "→ writing SAFE state: mode=MAXIMUM_SELF_CONSUMPTION, cutoff_raw=%d, exp_cap=5kW",
        ceiling_raw,
    )
    ok = True
    # Mode first — get out of whatever weird state we were in.
    ok &= await _assert_mode_2(controller)
    ok &= await _write_cutoff_raw(controller, ceiling_raw)
    ok &= await controller._write_u32(REG_GRID_EXPORT_LIMIT, 5000)
    return ok


# ── Sub-probes ──────────────────────────────────────────────────


async def _probe_1_rewrite_frequency(
    controller: SigenergyController,
    samples: list[Sample],
    writes: list[WriteEvent],
    t0: float,
    ceiling_raw: int,
) -> None:
    """Write 40047 alternating (soc+1)*10 and (soc+2)*10 every
    P1_WRITE_INTERVAL_S for P1_DURATION_S. Readback after each write.

    Pass: every write returns True (modulo ≤1% transient jitter),
    readback within ±1 raw unit, no alarm bits flip during probe.
    """
    logger.info("Probe 1 / 4: REWRITE FREQUENCY (%ds, write every %.0fs)",
                P1_DURATION_S, P1_WRITE_INTERVAL_S)

    # Baseline
    await _sample_loop(controller, "p1_baseline", BASELINE_DURATION_S, samples, t0)

    # Read SOC to base the target values on
    pre = await controller.read_state()
    if pre is None or pre.soc_pct is None:
        logger.error("Probe 1: could not read SOC; skipping")
        return
    soc_raw_base = int(pre.soc_pct * 10)

    end = time.monotonic() + P1_DURATION_S
    toggle = 0
    while time.monotonic() < end:
        await touch_heartbeat()
        intended = soc_raw_base + (10 if toggle == 0 else 20)  # +1% or +2%
        toggle ^= 1
        write_ok = await _write_cutoff_raw(controller, intended)
        # Small delay to let firmware settle, then read back.
        await asyncio.sleep(0.1)
        readback = await _read_cutoff(controller)
        drift = (readback - intended) if readback is not None else None
        elapsed = time.monotonic() - t0
        writes.append(WriteEvent(
            ts=now_utc().isoformat(),
            elapsed_s=round(elapsed, 2),
            phase="p1_probe",
            intended_raw=intended,
            write_ok=write_ok,
            readback_raw=readback,
            drift_raw=drift,
        ))
        logger.info(
            "[p1 %5.1fs] write(40047=%d) ok=%s readback=%s drift=%s",
            elapsed, intended, write_ok, readback, drift,
        )
        # Sample telemetry alongside the write
        samples.append(await _sample(controller, "p1_probe", elapsed))
        # Sleep to next write tick
        await asyncio.sleep(max(0.0, P1_WRITE_INTERVAL_S - 0.15))

    # Recovery
    await _write_safe_state(controller, ceiling_raw)
    await _sample_loop(controller, "p1_recovery", RECOVERY_DURATION_S, samples, t0)


async def _probe_2_cutoff_below_soc(
    controller: SigenergyController,
    samples: list[Sample],
    t0: float,
    ceiling_raw: int,
) -> None:
    """Write cutoff = (current_soc - 5)%. Observe battery for 60 s.

    Pass: |battery_kw| < P2_MAX_BATTERY_KW_ABS throughout — inverter
    idles (semantic "don't charge above cutoff" without forced discharge).
    """
    logger.info("Probe 2 / 4: CUTOFF BELOW SOC (%ds dwell)", P2_DURATION_S)

    await _sample_loop(controller, "p2_baseline", BASELINE_DURATION_S, samples, t0)

    pre = await controller.read_state()
    if pre is None or pre.soc_pct is None:
        logger.error("Probe 2: could not read SOC; skipping")
        return
    target_raw = int((pre.soc_pct - 5.0) * 10)
    if target_raw <= 0:
        logger.error(
            "Probe 2: SOC (%.1f%%) too low to write cutoff-5%%; skipping",
            pre.soc_pct,
        )
        return
    logger.warning("Probe 2: writing cutoff_raw=%d (5%% below SOC=%.1f%%)",
                   target_raw, pre.soc_pct)
    await _write_cutoff_raw(controller, target_raw)

    await _sample_loop(controller, "p2_probe", P2_DURATION_S, samples, t0)

    # Recovery
    await _write_safe_state(controller, ceiling_raw)
    await _sample_loop(controller, "p2_recovery", RECOVERY_DURATION_S, samples, t0)


async def _probe_3_cutoff_at_soc(
    controller: SigenergyController,
    samples: list[Sample],
    t0: float,
    ceiling_raw: int,
) -> None:
    """Write cutoff = current_soc_raw exactly. Observe 60 s for
    oscillation at the boundary.

    Pass: |battery_kw| < P3_MAX_BATTERY_KW_ABS (tighter threshold than
    P2 because we expect dead stability, not mere idle).
    """
    logger.info("Probe 3 / 4: CUTOFF AT EXACT SOC (%ds dwell)", P3_DURATION_S)

    await _sample_loop(controller, "p3_baseline", BASELINE_DURATION_S, samples, t0)

    pre = await controller.read_state()
    if pre is None or pre.soc_pct is None:
        logger.error("Probe 3: could not read SOC; skipping")
        return
    target_raw = int(pre.soc_pct * 10)
    logger.warning("Probe 3: writing cutoff_raw=%d (= current SOC=%.1f%%)",
                   target_raw, pre.soc_pct)
    await _write_cutoff_raw(controller, target_raw)

    await _sample_loop(controller, "p3_probe", P3_DURATION_S, samples, t0)

    await _write_safe_state(controller, ceiling_raw)
    await _sample_loop(controller, "p3_recovery", RECOVERY_DURATION_S, samples, t0)


async def _probe_4_supersession(
    controller: SigenergyController,
    samples: list[Sample],
    writes: list[WriteEvent],
    t0: float,
    ceiling_raw: int,
) -> None:
    """40 iterations at 5 s cadence: write ceiling, then immediately
    write (soc+2)%, then read back. Pass: every readback reflects the
    second (tick-path) write, never the ceiling.
    """
    logger.info("Probe 4 / 4: SUPERSESSION vs startup (%ds, %d iterations)",
                P4_DURATION_S, int(P4_DURATION_S / P4_WRITE_INTERVAL_S))

    await _sample_loop(controller, "p4_baseline", BASELINE_DURATION_S, samples, t0)

    end = time.monotonic() + P4_DURATION_S
    iteration = 0
    while time.monotonic() < end:
        await touch_heartbeat()
        iteration += 1

        # First write: the "startup" write (ceiling)
        await _write_cutoff_raw(controller, ceiling_raw)
        await asyncio.sleep(0.1)

        # Second write: the "tick path" write (dynamic target)
        pre = await controller.read_state()
        if pre is None or pre.soc_pct is None:
            logger.warning("Probe 4 iteration %d: could not read SOC", iteration)
            await asyncio.sleep(P4_WRITE_INTERVAL_S)
            continue
        target_raw = int((pre.soc_pct + 2.0) * 10)
        write_ok = await _write_cutoff_raw(controller, target_raw)
        await asyncio.sleep(0.1)

        # Read back and see which write won
        readback = await _read_cutoff(controller)
        drift = (readback - target_raw) if readback is not None else None
        elapsed = time.monotonic() - t0
        writes.append(WriteEvent(
            ts=now_utc().isoformat(),
            elapsed_s=round(elapsed, 2),
            phase="p4_probe",
            intended_raw=target_raw,
            write_ok=write_ok,
            readback_raw=readback,
            drift_raw=drift,
        ))
        logger.info(
            "[p4 iter=%d] ceiling=%d then target=%d → readback=%s drift=%s",
            iteration, ceiling_raw, target_raw, readback, drift,
        )
        # Sample telemetry alongside
        samples.append(await _sample(controller, "p4_probe", elapsed))
        await asyncio.sleep(max(0.0, P4_WRITE_INTERVAL_S - 0.3))

    await _write_safe_state(controller, ceiling_raw)
    await _sample_loop(controller, "p4_recovery", RECOVERY_DURATION_S, samples, t0)


# ── Verdicts ────────────────────────────────────────────────────


def _phase(samples: list[Sample], name: str) -> list[Sample]:
    return [s for s in samples if s.phase == name]


def _phase_writes(writes: list[WriteEvent], name: str) -> list[WriteEvent]:
    return [w for w in writes if w.phase == name]


def _mean(xs: list[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def _max_abs(xs: list[float | None]) -> float | None:
    vals = [abs(x) for x in xs if x is not None]
    return max(vals) if vals else None


def _alarm_bits_set(samples: list[Sample]) -> int:
    """Count of non-zero alarm register readings across all samples."""
    n = 0
    for s in samples:
        for a in s.alarms:
            if a is not None and a != 0:
                n += 1
    return n


def _verdict_p1(samples: list[Sample], writes: list[WriteEvent]) -> str:
    probe_samples = _phase(samples, "p1_probe")
    probe_writes = _phase_writes(writes, "p1_probe")
    if not probe_writes:
        return "INCONCLUSIVE (no writes)"
    n_writes = len(probe_writes)
    n_failed = sum(1 for w in probe_writes if not w.write_ok)
    fail_frac = n_failed / n_writes
    drifts = [abs(w.drift_raw) for w in probe_writes if w.drift_raw is not None]
    max_drift = max(drifts) if drifts else None
    n_alarms = _alarm_bits_set(probe_samples)
    pass_write = fail_frac <= P1_MAX_WRITE_FAILURE_FRAC
    pass_drift = max_drift is None or max_drift <= P1_READBACK_TOLERANCE_RAW
    pass_alarm = n_alarms <= P1_MAX_ALARMS_OK
    if pass_write and pass_drift and pass_alarm:
        return (
            f"PASS (writes={n_writes}, fail_frac={fail_frac:.3f}, "
            f"max_drift={max_drift}, alarm_hits={n_alarms})"
        )
    reasons = []
    if not pass_write:
        reasons.append(f"write fail_frac={fail_frac:.3f} > {P1_MAX_WRITE_FAILURE_FRAC}")
    if not pass_drift:
        reasons.append(f"max_drift={max_drift} > {P1_READBACK_TOLERANCE_RAW}")
    if not pass_alarm:
        reasons.append(f"alarm_hits={n_alarms} > 0 — inspect dump")
    return "FAIL (" + "; ".join(reasons) + ")"


def _verdict_p2(samples: list[Sample]) -> str:
    probe = _phase(samples, "p2_probe")
    if not probe:
        return "INCONCLUSIVE (no samples)"
    max_abs_bat = _max_abs([s.battery_kw for s in probe]) or 0.0
    mean_bat = _mean([s.battery_kw for s in probe]) or 0.0
    n_alarms = _alarm_bits_set(probe)
    if max_abs_bat <= P2_MAX_BATTERY_KW_ABS and n_alarms == 0:
        return (
            f"PASS (max|bat|={max_abs_bat:.3f}kW, mean_bat={mean_bat:+.3f}kW, "
            f"alarms=0) — inverter idles with cutoff below SOC"
        )
    if mean_bat < -0.1:
        return (
            f"FAIL — battery DISCHARGING (mean={mean_bat:+.2f}kW). "
            "Clamp required in set_charge_cut_off_soc."
        )
    reasons = []
    if max_abs_bat > P2_MAX_BATTERY_KW_ABS:
        reasons.append(f"max|bat|={max_abs_bat:.2f}kW > {P2_MAX_BATTERY_KW_ABS}")
    if n_alarms:
        reasons.append(f"alarm_hits={n_alarms}")
    return "FAIL (" + "; ".join(reasons) + ")"


def _verdict_p3(samples: list[Sample]) -> str:
    probe = _phase(samples, "p3_probe")
    if not probe:
        return "INCONCLUSIVE (no samples)"
    max_abs_bat = _max_abs([s.battery_kw for s in probe]) or 0.0
    n_alarms = _alarm_bits_set(probe)
    if max_abs_bat <= P3_MAX_BATTERY_KW_ABS and n_alarms == 0:
        return f"PASS (max|bat|={max_abs_bat:.3f}kW, alarms=0) — boundary stable"
    reasons = []
    if max_abs_bat > P3_MAX_BATTERY_KW_ABS:
        reasons.append(
            f"max|bat|={max_abs_bat:.3f}kW > {P3_MAX_BATTERY_KW_ABS} "
            "(oscillation — use current_soc+0.1 as hold value)"
        )
    if n_alarms:
        reasons.append(f"alarm_hits={n_alarms}")
    return "FAIL (" + "; ".join(reasons) + ")"


def _verdict_p4(writes: list[WriteEvent]) -> str:
    probe = _phase_writes(writes, "p4_probe")
    if not probe:
        return "INCONCLUSIVE (no writes)"
    # We expect the readback to match the second write (the target), not
    # the first write (the ceiling). Drift within tolerance → the
    # tick-path value won.
    drifts = [abs(w.drift_raw) for w in probe if w.drift_raw is not None]
    if not drifts:
        return "INCONCLUSIVE (no readbacks succeeded)"
    max_drift = max(drifts)
    n_reverted = sum(1 for w in probe if w.drift_raw is not None and abs(w.drift_raw) > P4_READBACK_TOLERANCE_RAW)
    if max_drift <= P4_READBACK_TOLERANCE_RAW:
        return (
            f"PASS (iterations={len(probe)}, max_drift={max_drift} "
            f"≤ {P4_READBACK_TOLERANCE_RAW}) — tick write supersedes startup"
        )
    return (
        f"FAIL ({n_reverted}/{len(probe)} iterations reverted toward ceiling; "
        f"max_drift={max_drift}). Split assert_battery_soc_limits in Commit 2."
    )


def _summarise(
    samples: list[Sample], writes: list[WriteEvent], ceiling_raw: int
) -> None:
    print("\n══════ SUMMARY ══════")
    print(f"  Safe-state ceiling raw = {ceiling_raw} ({ceiling_raw / 10:.1f}%)")
    print()
    print("  Probe 1 (rewrite frequency):   " + _verdict_p1(samples, writes))
    print("  Probe 2 (cutoff below SOC):    " + _verdict_p2(samples))
    print("  Probe 3 (cutoff at SOC):       " + _verdict_p3(samples))
    print("  Probe 4 (supersession):        " + _verdict_p4(writes))

    # Quick environmental summary for each probe phase
    def _line(name: str) -> None:
        ss = _phase(samples, name)
        if not ss:
            return
        print(
            f"    {name:<13}: "
            f"pv={(_mean([s.pv_kw for s in ss]) or 0):.2f}kW, "
            f"bat={(_mean([s.battery_kw for s in ss]) or 0):+.2f}kW, "
            f"grid={(_mean([s.grid_kw for s in ss]) or 0):+.2f}kW"
        )
    print("\n  Phase means:")
    for name in (
        "p1_baseline", "p1_probe", "p1_recovery",
        "p2_baseline", "p2_probe", "p2_recovery",
        "p3_baseline", "p3_probe", "p3_recovery",
        "p4_baseline", "p4_probe", "p4_recovery",
    ):
        _line(name)


# ── Entry points ────────────────────────────────────────────────


async def run(
    config_path: Path,
    samples_dump: Path | None,
    writes_dump: Path | None,
) -> int:
    config = load_config(config_path)
    controller = SigenergyController(config.sigenergy, config.battery)
    ceiling_raw = int(config.battery.soc_ceiling_pct * 10)

    logger.info("Connecting to Sigenergy at %s:%d ...",
                config.sigenergy.host, config.sigenergy.port)
    if not await controller.connect():
        logger.error("Modbus connect failed — is the service still holding the socket?")
        return 2

    samples: list[Sample] = []
    writes: list[WriteEvent] = []
    touched_cutoff = False

    try:
        # Ensure Remote EMS on — writes to 40031/40047 need it.
        if not await controller._read_input_u16(REG_REMOTE_EMS_ENABLE):
            logger.warning("Remote EMS was disabled — enabling for the probe.")
            if not await controller.enable_remote_ems():
                logger.error("Could not enable Remote EMS — aborting.")
                return 3
        controller._remote_ems_enabled = True

        # Pre-flight
        preflight = await controller.read_state()
        if preflight is None:
            logger.error("Could not read pre-flight state — aborting.")
            return 4
        pv = preflight.pv_power_kw or 0.0
        soc = preflight.soc_pct or 0.0
        logger.info(
            "Pre-flight: PV=%.2fkW SOC=%.1f%% load=%.2fkW grid=%.2fkW "
            "(safe window: PV≥%.1f, %.0f≤SOC≤%.0f)",
            pv, soc,
            preflight.house_load_kw or 0.0,
            preflight.grid_power_kw or 0.0,
            MIN_PV_KW, MIN_SOC_PCT, MAX_SOC_PCT,
        )
        if pv < MIN_PV_KW:
            logger.error("PV (%.2fkW) < %.1fkW — retry at midday.", pv, MIN_PV_KW)
            return 5
        if not (MIN_SOC_PCT <= soc <= MAX_SOC_PCT):
            logger.error("SOC (%.1f%%) outside [%.0f, %.0f]%% — retry later.",
                         soc, MIN_SOC_PCT, MAX_SOC_PCT)
            return 6

        # Pin mode 2 before starting. All four probes assume mode = 2.
        await _assert_mode_2(controller)

        t0 = time.monotonic()
        touched_cutoff = True

        await _probe_1_rewrite_frequency(controller, samples, writes, t0, ceiling_raw)
        await _probe_2_cutoff_below_soc(controller, samples, t0, ceiling_raw)
        await _probe_3_cutoff_at_soc(controller, samples, t0, ceiling_raw)
        await _probe_4_supersession(controller, samples, writes, t0, ceiling_raw)

        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.warning("Interrupted — reverting to safe state.")
        return 130
    except Exception:
        logger.exception("Probe crashed — reverting to safe state.")
        return 1
    finally:
        if touched_cutoff:
            try:
                await _write_safe_state(controller, ceiling_raw)
            except Exception:
                logger.exception("Safe-state revert FAILED — relying on watchdog.")
        if samples_dump and samples:
            samples_dump.write_text(
                "\n".join(json.dumps(asdict(s)) for s in samples) + "\n"
            )
            logger.info("Wrote %d samples to %s", len(samples), samples_dump)
        if writes_dump and writes:
            writes_dump.write_text(
                "\n".join(json.dumps(asdict(w)) for w in writes) + "\n"
            )
            logger.info("Wrote %d write-events to %s", len(writes), writes_dump)
        _summarise(samples, writes, ceiling_raw)
        await controller.disconnect()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(
        description="Sigenergy reg-40047 (charge_cut_off_soc) probe suite."
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), type=Path)
    p.add_argument(
        "--samples-dump",
        default="/var/lib/energy-optimiser/probe_charge_cutoff.ndjson",
        type=lambda s: Path(s) if s else None,
        help="NDJSON file for per-sample telemetry (empty to skip).",
    )
    p.add_argument(
        "--writes-dump",
        default="/var/lib/energy-optimiser/probe_charge_cutoff_writes.ndjson",
        type=lambda s: Path(s) if s else None,
        help="NDJSON file for per-write events (empty to skip).",
    )
    args = p.parse_args()
    return asyncio.run(run(args.config, args.samples_dump, args.writes_dump))


if __name__ == "__main__":
    sys.exit(main())
