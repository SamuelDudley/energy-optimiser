"""One-shot hardware probe: observe Sigenergy mode 5 behaviour under PV surplus.

The question this resolves:

    In mode 5 (DISCHARGING_PV_FIRST) with discharge_cap = 5 kW and
    export_cap = 5 kW, when PV > load + 5 kW, what happens to the
    surplus PV?

      Option A: surplus charges the battery (enables the S2
                "export-first, battery soaks remainder" architecture).
      Option B: MPPT curtails the surplus (S2 unworkable; back to
                tuning mode 4 caps).

The script commands mode 5 for ~120 s while sampling telemetry at ~1 Hz,
then reverts to mode 2 (MAXIMUM_SELF_CONSUMPTION). Throughout, it
refreshes the heartbeat file so the external watchdog doesn't trigger
fallback. If the script crashes or is interrupted, the `finally` block
still writes mode 2 back — and if that too fails, the watchdog's 90 s
stale window is the last line of defence.

Run from inside the optimiser image (it depends on the same pymodbus
client the service uses), after stopping the service to release the
Modbus TCP connection:

    docker compose stop optimiser
    docker run --rm --network host \\
        -v energy-optimiser_optimiser-data:/var/lib/energy-optimiser \\
        -v /etc/energy-optimiser:/etc/energy-optimiser:ro \\
        energy-optimiser-optimiser python -m optimiser.probe_mode5
    docker compose start optimiser

Total runtime: ~2.5 minutes. Safe window: PV must be healthy; script
aborts if PV < 6 kW or SOC is near the floor / ceiling.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .clients.sigenergy import (
    REG_ESS_MAX_CHARGING_LIMIT,
    REG_ESS_MAX_DISCHARGING_LIMIT,
    REG_GRID_EXPORT_LIMIT,
    REG_INVERTER_PV1_CURRENT,
    REG_INVERTER_PV1_VOLTAGE,
    REG_INVERTER_PV2_CURRENT,
    REG_INVERTER_PV2_VOLTAGE,
    REG_INVERTER_PV3_CURRENT,
    REG_INVERTER_PV3_VOLTAGE,
    REG_INVERTER_PV4_CURRENT,
    REG_INVERTER_PV4_VOLTAGE,
    REG_REMOTE_EMS_CONTROL_MODE,
    REG_REMOTE_EMS_ENABLE,
    SigenergyController,
)
from .config import load_config
from .time_utils import now_utc
from .types import RemoteEMSControlMode

logger = logging.getLogger("probe_mode5")

HEARTBEAT_PATH = Path("/var/lib/energy-optimiser/heartbeat")
DEFAULT_CONFIG_PATH = Path("/etc/energy-optimiser/config.toml")

# Probe schedule (seconds)
BASELINE_DURATION_S = 10
PROBE_DURATION_S = 120
RECOVERY_DURATION_S = 15
SAMPLE_INTERVAL_S = 1.0

# Safety thresholds — refuse to run outside these bounds.
MIN_PV_KW = 6.0          # Need enough surplus to observe the overflow behaviour.
MIN_SOC_PCT = 25.0       # Keep headroom below ceiling so the battery can charge.
MAX_SOC_PCT = 85.0       # Keep headroom above floor so it can discharge if firmware insists.

# Probe target state.
PROBE_MODE = RemoteEMSControlMode.COMMAND_DISCHARGING_PV_FIRST  # = 5
PROBE_DISCHARGE_CAP_KW = 5.0
PROBE_EXPORT_CAP_KW = 5.0


@dataclass(slots=True)
class Sample:
    ts: str
    elapsed_s: float
    phase: str  # baseline | probe | recovery
    ems_mode: int | None
    soc_pct: float | None
    pv_kw: float | None
    battery_kw: float | None  # + charge, − discharge
    grid_kw: float | None     # + import, − export
    house_load_kw: float | None
    mppt1_v: float | None
    mppt1_a: float | None
    mppt2_v: float | None
    mppt2_a: float | None
    mppt3_v: float | None
    mppt3_a: float | None
    mppt4_v: float | None
    mppt4_a: float | None


async def touch_heartbeat() -> None:
    """Refresh the heartbeat file so the watchdog sidecar doesn't fire."""
    try:
        HEARTBEAT_PATH.touch(exist_ok=True)
    except OSError as exc:
        logger.warning("heartbeat touch failed: %s", exc)


async def _sample(controller: SigenergyController, phase: str, elapsed: float) -> Sample:
    """Capture one telemetry sample with MPPT detail.

    Uses the controller's best-effort read helpers so an individual
    register read failure doesn't abort the probe — we'd rather capture
    partial data than nothing.
    """
    state = await controller.read_state()
    inv = controller._config.inverter_slave_id

    async def _s16(reg: int, gain: int) -> float | None:
        return await controller._read_input_s16(reg, gain=gain, slave_id=inv)

    # MPPT voltages are S16 gain=10 V (per register table); currents S16 gain=100 A.
    mppt1_v = await _s16(REG_INVERTER_PV1_VOLTAGE, 10)
    mppt1_a = await _s16(REG_INVERTER_PV1_CURRENT, 100)
    mppt2_v = await _s16(REG_INVERTER_PV2_VOLTAGE, 10)
    mppt2_a = await _s16(REG_INVERTER_PV2_CURRENT, 100)
    mppt3_v = await _s16(REG_INVERTER_PV3_VOLTAGE, 10)
    mppt3_a = await _s16(REG_INVERTER_PV3_CURRENT, 100)
    mppt4_v = await _s16(REG_INVERTER_PV4_VOLTAGE, 10)
    mppt4_a = await _s16(REG_INVERTER_PV4_CURRENT, 100)

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
        mppt1_v=mppt1_v, mppt1_a=mppt1_a,
        mppt2_v=mppt2_v, mppt2_a=mppt2_a,
        mppt3_v=mppt3_v, mppt3_a=mppt3_a,
        mppt4_v=mppt4_v, mppt4_a=mppt4_a,
    )


async def _sample_loop(
    controller: SigenergyController,
    phase: str,
    duration_s: float,
    out: list[Sample],
    t0: float,
) -> None:
    """Sample at 1 Hz for `duration_s`, appending to `out`. Heartbeat-refreshed."""
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
        # Sleep until the next tick, respecting elapsed time of the read.
        await asyncio.sleep(max(0.0, SAMPLE_INTERVAL_S - 0.05))


async def _write_probe_state(controller: SigenergyController) -> bool:
    """Write mode 5 + caps. Order: caps first, then mode."""
    logger.warning("→ writing PROBE state: mode=%s, disc_cap=%skW, exp_cap=%skW",
                   PROBE_MODE.name, PROBE_DISCHARGE_CAP_KW, PROBE_EXPORT_CAP_KW)
    ok = True
    ok &= await controller._write_u32(
        REG_ESS_MAX_DISCHARGING_LIMIT, int(PROBE_DISCHARGE_CAP_KW * 1000)
    )
    ok &= await controller._write_u32(
        REG_GRID_EXPORT_LIMIT, int(PROBE_EXPORT_CAP_KW * 1000)
    )
    # Reset charge cap to a safe max so a later dispatch doesn't inherit
    # a stale value (mode 5 ignores it, but tidy is tidy).
    ok &= await controller._write_u32(REG_ESS_MAX_CHARGING_LIMIT, 0)
    ok &= await controller._write_u16(REG_REMOTE_EMS_CONTROL_MODE, PROBE_MODE.value)
    return ok


async def _write_safe_state(controller: SigenergyController) -> bool:
    """Revert to mode 2 + export cap 5 kW (service default). Always runs."""
    logger.warning("→ writing SAFE state: mode=MAXIMUM_SELF_CONSUMPTION, exp_cap=5kW")
    ok = True
    # Mode first here — get out of mode 5 as quickly as possible.
    ok &= await controller._write_u16(
        REG_REMOTE_EMS_CONTROL_MODE,
        RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION.value,
    )
    ok &= await controller._write_u32(REG_GRID_EXPORT_LIMIT, 5000)
    ok &= await controller._write_u32(REG_ESS_MAX_DISCHARGING_LIMIT, 0)
    ok &= await controller._write_u32(REG_ESS_MAX_CHARGING_LIMIT, 0)
    return ok


def _summarise(samples: list[Sample]) -> None:
    """Print a terse verdict."""

    def _phase(name: str) -> list[Sample]:
        return [s for s in samples if s.phase == name]

    def _mean(xs: list[float | None]) -> float | None:
        vals = [x for x in xs if x is not None]
        return sum(vals) / len(vals) if vals else None

    def _line(name: str) -> None:
        ss = _phase(name)
        if not ss:
            print(f"  {name:<10}: no samples")
            return
        print(
            f"  {name:<10}: "
            f"pv={_mean([s.pv_kw for s in ss]):.2f}kW, "
            f"bat={_mean([s.battery_kw for s in ss]):+.2f}kW, "
            f"grid={_mean([s.grid_kw for s in ss]):+.2f}kW, "
            f"load={_mean([s.house_load_kw for s in ss]):.2f}kW"
        )

    print("\n══════ SUMMARY ══════")
    _line("baseline")
    _line("probe")
    _line("recovery")

    # Verdict
    probe = _phase("probe")
    if probe:
        bat_mean = _mean([s.battery_kw for s in probe]) or 0.0
        grid_mean = _mean([s.grid_kw for s in probe]) or 0.0
        pv_mean = _mean([s.pv_kw for s in probe]) or 0.0
        print("\nVerdict:")
        if bat_mean > 0.3:
            print("  → Battery is CHARGING during mode 5 with surplus PV.")
            print("    This is OPTION A: surplus PV absorbs into battery.")
            print("    Mode 5 is safe for the S2 'export-first' architecture.")
        elif bat_mean < -0.3:
            print("  → Battery is DISCHARGING during mode 5.")
            print("    Firmware is honouring the discharge command literally;")
            print("    check whether PV or battery is supplying the export.")
        else:
            print("  → Battery is IDLE (±0.3 kW). Firmware likely CURTAILED PV.")
            print("    Inspect MPPT voltages in the NDJSON dump for confirmation.")
            print("    Option B: mode 5 is NOT suitable for S2.")
        if grid_mean > 0.3:
            print(f"  ⚠ Grid import detected ({grid_mean:+.2f}kW mean). Not the export-first behaviour.")
        elif grid_mean < -0.3:
            print(f"  ✓ Net export active ({grid_mean:+.2f}kW mean; negative = export).")
        print(f"  Avg PV: {pv_mean:.2f}kW (forecast-vs-delivered tells you if MPPT curtailed).")


async def run(config_path: Path, dump_path: Path | None) -> int:
    config = load_config(config_path)
    controller = SigenergyController(config.sigenergy, config.battery)

    logger.info("Connecting to Sigenergy at %s:%d ...",
                config.sigenergy.host, config.sigenergy.port)
    if not await controller.connect():
        logger.error("Modbus connect failed — is the service still holding the socket?")
        return 2

    samples: list[Sample] = []
    probe_started = False

    try:
        # Ensure Remote EMS is enabled (service normally keeps it on; be defensive).
        if not await controller._read_input_u16(REG_REMOTE_EMS_ENABLE):
            logger.warning("Remote EMS was disabled — enabling for the probe.")
            if not await controller.enable_remote_ems():
                logger.error("Could not enable Remote EMS — aborting.")
                return 3
        controller._remote_ems_enabled = True

        # Pre-flight: is the system in a state where this probe is meaningful?
        preflight = await controller.read_state()
        if preflight is None:
            logger.error("Could not read pre-flight state — aborting.")
            return 4
        pv = preflight.pv_power_kw or 0.0
        soc = preflight.soc_pct or 0.0
        logger.info("Pre-flight: PV=%.2fkW SOC=%.1f%% load=%.2fkW grid=%.2fkW",
                    pv, soc, preflight.house_load_kw or 0.0,
                    preflight.grid_power_kw or 0.0)
        if pv < MIN_PV_KW:
            logger.error("PV (%.2f kW) < %.1f kW — no surplus to observe. Try again at midday.",
                         pv, MIN_PV_KW)
            return 5
        if not (MIN_SOC_PCT <= soc <= MAX_SOC_PCT):
            logger.error("SOC (%.1f%%) outside [%.0f, %.0f]%% window. Aborting.",
                         soc, MIN_SOC_PCT, MAX_SOC_PCT)
            return 6

        t0 = time.monotonic()

        # Phase 1: baseline in current mode.
        logger.info("Phase 1/3: BASELINE (%ds)", BASELINE_DURATION_S)
        await _sample_loop(controller, "baseline", BASELINE_DURATION_S, samples, t0)

        # Phase 2: write mode 5 and observe.
        logger.info("Phase 2/3: PROBE (%ds, mode 5)", PROBE_DURATION_S)
        if not await _write_probe_state(controller):
            logger.error("Could not write probe state — aborting before sampling.")
            return 7
        probe_started = True
        await _sample_loop(controller, "probe", PROBE_DURATION_S, samples, t0)

        # Phase 3: revert and confirm.
        logger.info("Phase 3/3: RECOVERY (%ds)", RECOVERY_DURATION_S)
        await _write_safe_state(controller)
        probe_started = False
        await _sample_loop(controller, "recovery", RECOVERY_DURATION_S, samples, t0)

        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.warning("Interrupted — reverting to safe state.")
        return 130
    except Exception:
        logger.exception("Probe crashed — reverting to safe state.")
        return 1
    finally:
        if probe_started:
            # Best-effort revert, even if we've already logged a failure.
            try:
                await _write_safe_state(controller)
            except Exception:
                logger.exception("Safe-state revert FAILED — relying on watchdog.")
        # Dump samples regardless so we can inspect a partial run.
        if dump_path and samples:
            dump_path.write_text(
                "\n".join(json.dumps(asdict(s)) for s in samples) + "\n"
            )
            logger.info("Wrote %d samples to %s", len(samples), dump_path)
        _summarise(samples)
        await controller.disconnect()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(description="Sigenergy mode-5 surplus-PV probe.")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), type=Path)
    p.add_argument(
        "--dump",
        default="/var/lib/energy-optimiser/probe_mode5.ndjson",
        type=lambda s: Path(s) if s else None,
        help="NDJSON file to write samples to (empty to skip).",
    )
    args = p.parse_args()
    return asyncio.run(run(args.config, args.dump))


if __name__ == "__main__":
    sys.exit(main())
