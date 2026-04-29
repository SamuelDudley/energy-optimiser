"""Hardware probe: detect accidental PV curtailment in mode 2.

Question this probe answers: are the operational settings the service
writes per tick (the LP-trim on 40032, the DNSP-bounded export cap on
40038) accidentally throttling available PV? If so, an "uncapped" mode-2
configuration should report a higher steady-state ``pv_power_kw`` at the
same irradiance / SOC than the baseline reading we capture just before
applying it.

Configuration applied:

    mode (40031)        = MAXIMUM_SELF_CONSUMPTION (2)
    charge cap (40032)  = ``battery.max_dc_charge_kw``  — soak everything
    export cap (40038)  = ``--export-cap-kw``           — overflow path
    cutoff   (40047)    = ``battery.soc_ceiling_pct``   — re-asserted

The mode-2 cascade is ``PV → house → battery (cap 40032) → export
(cap 40038) → curtail``. With both caps maxed, the only thing that can
still curtail is the inverter's own MPPT throttling when battery and
export are both saturated — which is exactly the underlying available
PV we want to measure.

Phases:

    Baseline (30 s) — observe whatever 40032 / 40038 the running service
                      last wrote (the probe stops the optimiser
                      container, so this captures the LP's last-tick
                      command "frozen" by the cascade).
    Uncap   (60 s)  — write the uncapped state, sample.
    Recovery (10 s) — revert and observe.

A mean-PV uplift (uncap − baseline) above the noise floor implies the
operational settings are leaving energy on the table.

Safe-state revert in ``finally``:
    mode 2, 40032 = max_dc_charge_kw, 40038 = battery.export_limit_kw
    (DNSP cap), 40047 = battery.soc_ceiling_pct, Remote EMS = enabled.
This is the same shape as ``set_fallback`` / ``assert_battery_soc_limits``
— the running service re-enters cleanly on its next tick.

DNSP note: the default ``--export-cap-kw`` is ``max_discharge_kw`` (the
inverter's AC-tie limit, ~10 kW), which exceeds the configured DNSP
limit (``export_limit_kw``, typically 5 kW). The probe is short and the
overflow only flows when battery + house can't absorb the surplus; on
the recovery path the DNSP cap is restored. To run strictly within the
DNSP envelope, pass ``--export-cap-kw <export_limit_kw>``.

Run:
    docker compose stop optimiser
    docker run --rm --network host \\
        -v energy-optimiser_optimiser-data:/var/lib/energy-optimiser \\
        -v /home/dudley/code/energy-optimiser/config.toml:/etc/energy-optimiser/config.toml:ro \\
        energy-optimiser-optimiser python -m optimiser.probe_uncapped_pv
    docker compose start optimiser

Total runtime: ~110 s plus pre-flight.
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

logger = logging.getLogger("probe_uncapped_pv")

HEARTBEAT_PATH = Path("/var/lib/energy-optimiser/heartbeat")
DEFAULT_CONFIG_PATH = Path("/etc/energy-optimiser/config.toml")

BASELINE_DURATION_S = 30
UNCAP_DURATION_S = 60
RECOVERY_DURATION_S = 10
SAMPLE_INTERVAL_S = 1.0

# Steady-state window — last N seconds of a phase used for the verdict.
# Cascade settles in 2-3 s after a register write; 10 s gives plenty of
# margin and averages over typical irradiance jitter.
SETTLE_WINDOW_S = 10

# Pre-flight gates.
#   MIN_PV_KW: below this, no curtailment is plausibly happening — the
#     cascade has slack and the test is uninformative.
#   MIN/MAX_SOC_PCT: keep enough headroom that the battery can absorb
#     ~5 minutes of full-rate charging without saturating mid-probe
#     (which would shift the cascade to export and confound the
#     comparison). Lower bound is conservative — fallback re-entry from
#     a low-SOC probe is fine but uninteresting.
MIN_PV_KW = 4.0
MIN_SOC_PCT = 20.0
MAX_SOC_PCT = 75.0

# Verdict threshold: uplift below this is dismissed as cascade settling
# / irradiance jitter. Inverter PV register precision is ~0.05 kW; 0.3
# absorbs short-period cloud / sample noise.
PV_UPLIFT_NOISE_KW = 0.3


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


@dataclass(slots=True)
class PhaseStats:
    phase: str
    n_samples: int
    pv_mean: float | None
    bat_mean: float | None
    grid_mean: float | None
    house_mean: float | None
    soc_mean: float | None


@dataclass(slots=True)
class RegisterReadback:
    label: str
    elapsed_s: float
    charge_cap_raw: int | None  # 40032 — gain=1000, kW
    export_cap_raw: int | None  # 40038 — gain=1000, kW
    cutoff_raw: int | None      # 40047 — gain=10, %


@dataclass(slots=True)
class ProbeResult:
    stats: dict[str, PhaseStats] = field(default_factory=dict)
    readbacks: list[RegisterReadback] = field(default_factory=list)
    expected: dict[str, float] = field(default_factory=dict)
    ok_writes: bool = True


# ── helpers ─────────────────────────────────────────────────────


async def touch_heartbeat() -> None:
    try:
        HEARTBEAT_PATH.touch(exist_ok=True)
    except OSError as exc:
        logger.warning("heartbeat touch failed: %s", exc)


# Watchdog stale_seconds is 90 s (docker-compose.yml). Touching every 5 s
# keeps us 18× under the threshold even if a register write or read
# stalls for a few seconds. The background task runs from before
# pre-flight all the way through the finally-clause safe-state revert
# so no probe path can starve the heartbeat.
HEARTBEAT_INTERVAL_S = 5.0


async def _heartbeat_loop(stop: asyncio.Event) -> None:
    """Touch heartbeat continuously until ``stop`` is set."""
    await touch_heartbeat()  # immediate touch on start
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_S)
            return  # stop set during wait
        except TimeoutError:
            await touch_heartbeat()


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


async def _read_holding_u32(
    controller: SigenergyController, address: int
) -> int | None:
    """Read a U32 holding-register pair (hi-word first), inline helper.

    Sigenergy convention matches ``_write_u32``: high-word at the lower
    address. Local to the probe so we don't touch production code for
    what is purely a diagnostic register-readback path.
    """
    try:
        result = await controller._client.read_holding_registers(
            address=address, count=2, device_id=controller._config.slave_id,
        )
        if result.isError():
            return None
        return (result.registers[0] << 16) | result.registers[1]
    except Exception:
        logger.debug("U32 holding read failed at %d", address)
        return None


async def _readback(
    controller: SigenergyController, label: str, t0: float
) -> RegisterReadback:
    """Read 40032 / 40038 / 40047 raw — diagnostic snapshot of caps."""
    charge_cap = await _read_holding_u32(controller, REG_ESS_MAX_CHARGING_LIMIT)
    export_cap = await _read_holding_u32(controller, REG_GRID_EXPORT_LIMIT)
    cutoff = await controller._read_holding_u16(REG_CHARGE_CUTOFF_SOC)
    rb = RegisterReadback(
        label=label,
        elapsed_s=time.monotonic() - t0,
        charge_cap_raw=charge_cap,
        export_cap_raw=export_cap,
        cutoff_raw=cutoff,
    )
    logger.info(
        "[readback %s] 40032=%s 40038=%s 40047=%s",
        label, charge_cap, export_cap, cutoff,
    )
    return rb


async def _write_uncap_state(
    controller: SigenergyController,
    *,
    ceiling_pct: float,
    max_dc_charge_kw: float,
    export_cap_kw: float,
) -> bool:
    """Apply max-PV-absorption configuration."""
    logger.warning(
        "→ writing UNCAP state: mode=2, 40032=%.1fkW (max_dc), "
        "40038=%.1fkW, 40047=%.1f%%",
        max_dc_charge_kw, export_cap_kw, ceiling_pct,
    )
    cutoff_raw = max(0, min(1000, int(round(ceiling_pct * 10))))
    charge_raw = int(round(max_dc_charge_kw * 1000))
    export_raw = max(0, int(round(export_cap_kw * 1000)))
    ok = True
    # Cutoff first — ensures the BMS won't gate the uncapped 40032 below
    # the configured ceiling. Idempotent if already at ceiling.
    ok &= await controller._write_u16(REG_CHARGE_CUTOFF_SOC, cutoff_raw)
    # Cap-first then mode (same ordering rule as apply_lp_dispatch — no
    # transient where mode is new but cap is stale).
    ok &= await controller._write_u32(REG_ESS_MAX_CHARGING_LIMIT, charge_raw)
    ok &= await controller._write_u32(REG_GRID_EXPORT_LIMIT, export_raw)
    ok &= await controller._write_u16(
        REG_REMOTE_EMS_CONTROL_MODE,
        RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION.value,
    )
    return ok


async def _write_safe_state(
    controller: SigenergyController,
    *,
    ceiling_pct: float,
    max_dc_charge_kw: float,
    dnsp_export_kw: float,
) -> bool:
    """Restore the inverter to the canonical post-fallback safe state.

    Identical write set to ``set_fallback(positive_price)`` — mode 2,
    40032 at the inverter ceiling, 40038 at DNSP, 40047 at the configured
    SOC ceiling. The running service's next tick re-asserts the LP plan
    on top of this without surprise.
    """
    logger.warning(
        "→ writing SAFE state: mode=2, 40032=%.1fkW (max_dc), "
        "40038=%.1fkW (DNSP), 40047=%.1f%%",
        max_dc_charge_kw, dnsp_export_kw, ceiling_pct,
    )
    cutoff_raw = max(0, min(1000, int(round(ceiling_pct * 10))))
    charge_raw = int(round(max_dc_charge_kw * 1000))
    export_raw = max(0, int(round(dnsp_export_kw * 1000)))
    ok = True
    ok &= await controller._write_u16(REG_CHARGE_CUTOFF_SOC, cutoff_raw)
    ok &= await controller._write_u32(REG_ESS_MAX_CHARGING_LIMIT, charge_raw)
    ok &= await controller._write_u32(REG_GRID_EXPORT_LIMIT, export_raw)
    ok &= await controller._write_u16(
        REG_REMOTE_EMS_CONTROL_MODE,
        RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION.value,
    )
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
        soc_mean=_mean([s.soc_pct for s in win]),
    )


def _verdict(result: ProbeResult) -> str:
    base = result.stats.get("baseline")
    uncap = result.stats.get("uncap")
    if base is None or uncap is None:
        return "INCONCLUSIVE (missing phase stats)"
    if base.pv_mean is None or uncap.pv_mean is None:
        return "INCONCLUSIVE (PV telemetry None)"

    uplift = uncap.pv_mean - base.pv_mean
    bat_uplift = (uncap.bat_mean or 0.0) - (base.bat_mean or 0.0)
    grid_delta = (uncap.grid_mean or 0.0) - (base.grid_mean or 0.0)

    parts = [
        f"pv {base.pv_mean:.2f}→{uncap.pv_mean:.2f} (Δ={uplift:+.2f})",
        f"bat {(base.bat_mean or 0):+.2f}→{(uncap.bat_mean or 0):+.2f} "
        f"(Δ={bat_uplift:+.2f})",
        f"grid {(base.grid_mean or 0):+.2f}→{(uncap.grid_mean or 0):+.2f} "
        f"(Δ={grid_delta:+.2f})",
    ]

    if uplift > PV_UPLIFT_NOISE_KW:
        return (
            f"CURTAILMENT DETECTED — uncap recovered +{uplift:.2f} kW PV; "
            + ", ".join(parts)
        )
    return "NO CURTAILMENT — " + ", ".join(parts)


def _summarise(
    samples: list[Sample],
    verdict: str,
    readbacks: list[RegisterReadback],
    config_summary: dict[str, float],
) -> None:
    print("\n══════ SUMMARY ══════")
    print("  Config:")
    for k, v in config_summary.items():
        print(f"    {k:<30} = {v}")
    print()
    print(f"  Verdict: {verdict}")
    print("\n  Phase steady-state means (last 10 s):")
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
        soc = f"{st.soc_mean:.1f}" if st.soc_mean is not None else "—"
        print(
            f"    {phase:<12}  pv={pv:>5} bat={bat:>6} grid={grid:>6} "
            f"load={load:>5} soc={soc:>5}"
        )
    print("\n  Register readbacks (raw — 40032/40038 in W, 40047 in 0.1%):")
    for r in readbacks:
        print(
            f"    {r.label:<22} t={r.elapsed_s:6.1f}s  "
            f"40032={r.charge_cap_raw}  40038={r.export_cap_raw}  "
            f"40047={r.cutoff_raw}"
        )


# ── entry ───────────────────────────────────────────────────────


async def run(
    config_path: Path,
    samples_dump: Path | None,
    export_cap_kw_override: float | None,
) -> int:
    config = load_config(config_path)
    controller = SigenergyController(config.sigenergy, config.battery)
    ceiling_pct = config.battery.soc_ceiling_pct
    dnsp_export_kw = config.battery.export_limit_kw
    max_dc_charge_kw = config.battery.max_dc_charge_kw
    max_discharge_kw = config.battery.max_discharge_kw
    uncap_export_kw = (
        export_cap_kw_override
        if export_cap_kw_override is not None
        else max_discharge_kw
    )

    config_summary = {
        "soc_ceiling_pct": ceiling_pct,
        "max_dc_charge_kw": max_dc_charge_kw,
        "max_discharge_kw": max_discharge_kw,
        "dnsp_export_kw (restored on exit)": dnsp_export_kw,
        "uncap_export_kw (probe value)": uncap_export_kw,
    }
    if uncap_export_kw > dnsp_export_kw:
        logger.warning(
            "Probe export cap (%.1fkW) exceeds DNSP limit (%.1fkW). "
            "Overflow only flows briefly and only when battery + house "
            "can't absorb surplus. Restored to DNSP on exit.",
            uncap_export_kw, dnsp_export_kw,
        )

    # Start the heartbeat keep-alive BEFORE any potentially-slow op
    # (modbus connect, pre-flight reads). Watchdog has 90 s patience —
    # this covers us through the whole probe including the finally
    # branch's safe-state revert.
    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(heartbeat_stop))

    logger.info(
        "Connecting to Sigenergy at %s:%d ...",
        config.sigenergy.host, config.sigenergy.port,
    )
    if not await controller.connect():
        logger.error(
            "Modbus connect failed — is the optimiser still holding the socket?"
        )
        heartbeat_stop.set()
        await heartbeat_task
        return 2

    samples: list[Sample] = []
    readbacks: list[RegisterReadback] = []
    verdict = "DID NOT RUN"
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
            logger.error(
                "PV (%.2fkW) < %.1fkW — retry at higher irradiance.",
                pv, MIN_PV_KW,
            )
            return 5
        if not (MIN_SOC_PCT <= soc <= MAX_SOC_PCT):
            logger.error(
                "SOC (%.1f%%) outside [%.0f, %.0f]%% — retry later.",
                soc, MIN_SOC_PCT, MAX_SOC_PCT,
            )
            return 6

        t0 = time.monotonic()
        touched_state = True

        # ── Baseline: capture current cap state, observe what the
        #    inverter does with the running service's last commands.
        readbacks.append(await _readback(controller, "before_baseline", t0))
        await _sample_loop(
            controller, "baseline", BASELINE_DURATION_S, samples, t0,
        )

        # ── Uncap: write the max-absorption state.
        if not await _write_uncap_state(
            controller,
            ceiling_pct=ceiling_pct,
            max_dc_charge_kw=max_dc_charge_kw,
            export_cap_kw=uncap_export_kw,
        ):
            logger.error("Uncap write failed — aborting (safe state on exit).")
            return 7
        readbacks.append(await _readback(controller, "after_uncap_write", t0))
        await _sample_loop(
            controller, "uncap", UNCAP_DURATION_S, samples, t0,
        )
        readbacks.append(await _readback(controller, "end_of_uncap", t0))

        # ── Recovery: revert and observe.
        if not await _write_safe_state(
            controller,
            ceiling_pct=ceiling_pct,
            max_dc_charge_kw=max_dc_charge_kw,
            dnsp_export_kw=dnsp_export_kw,
        ):
            logger.error("Safe-state revert returned partial failure.")
        readbacks.append(await _readback(controller, "after_safe_revert", t0))
        await _sample_loop(
            controller, "recovery", RECOVERY_DURATION_S, samples, t0,
        )

        result = ProbeResult()
        result.stats["baseline"] = _stats(samples, "baseline", SETTLE_WINDOW_S)
        result.stats["uncap"] = _stats(samples, "uncap", SETTLE_WINDOW_S)
        result.stats["recovery"] = _stats(samples, "recovery", SETTLE_WINDOW_S)
        result.readbacks = readbacks
        verdict = _verdict(result)
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
                await _write_safe_state(
                    controller,
                    ceiling_pct=ceiling_pct,
                    max_dc_charge_kw=max_dc_charge_kw,
                    dnsp_export_kw=dnsp_export_kw,
                )
                # Re-assert the discharge floor / backup SOC limits
                # untouched by this probe but cheap and idempotent —
                # leaves the service entering the next tick with the
                # full canonical SOC-limit set.
                await controller.assert_battery_soc_limits()
            except Exception:
                logger.exception(
                    "Safe-state revert FAILED — relying on watchdog."
                )
        # One last touch right before tearing down the heartbeat task
        # so the gap between probe-exit and the optimiser container
        # restart starts from "fresh", giving the operator time to
        # `docker compose start optimiser`.
        await touch_heartbeat()
        heartbeat_stop.set()
        try:
            await heartbeat_task
        except Exception:
            logger.exception("heartbeat task teardown failed")
        if samples_dump and samples:
            samples_dump.write_text(
                "\n".join(json.dumps(asdict(s)) for s in samples) + "\n"
            )
            logger.info("Wrote %d samples to %s", len(samples), samples_dump)
        _summarise(samples, verdict, readbacks, config_summary)
        await controller.disconnect()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(
        description="Sigenergy uncapped-mode-2 PV curtailment probe.",
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), type=Path)
    p.add_argument(
        "--samples-dump",
        default="/var/lib/energy-optimiser/probe_uncapped_pv.ndjson",
        type=lambda s: Path(s) if s else None,
        help="NDJSON file for per-sample telemetry (empty to skip).",
    )
    p.add_argument(
        "--export-cap-kw",
        type=float,
        default=None,
        help=(
            "Export cap to write during the uncap phase. Default: "
            "battery.max_discharge_kw (~10 kW). Pass battery.export_limit_kw "
            "(~5 kW) to stay strictly within DNSP."
        ),
    )
    args = p.parse_args()
    return asyncio.run(run(args.config, args.samples_dump, args.export_cap_kw))


if __name__ == "__main__":
    sys.exit(main())
