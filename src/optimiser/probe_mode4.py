"""One-shot hardware probe: observe Sigenergy mode 4 with cap = physical max.

Follow-up to `probe_mode5`, which confirmed mode 5 curtails surplus PV.

The question this resolves:

    In mode 4 (COMMAND_CHARGING_PV_FIRST) with charge_cap = max_dc_charge_kw
    (13 kW) and export_cap = 5 kW, when PV > load + 5 kW, what happens?

      Option A (cap is a CEILING): battery absorbs all available PV
                surplus up to 13 kW, no grid draw, no MPPT curtailment.
                This is the "use all PV" answer for S1.
      Option B (cap is a TARGET): inverter tries to fill 13 kW from
                whatever source — since PV only provides ~8 kW surplus,
                it pulls 5 kW from grid to top up. BAD.

Runs for 120 s with continuous 1-Hz sampling, then reverts to mode 2.
Same safety envelope as probe_mode5: heartbeat-refresh, finally-block
safe-state write, watchdog as last resort.

Run:
    docker compose stop optimiser
    docker run --rm --network host \\
        -v energy-optimiser_optimiser-data:/var/lib/energy-optimiser \\
        -v /home/dudley/code/energy-optimiser/config.toml:/etc/energy-optimiser/config.toml:ro \\
        energy-optimiser-optimiser python -m optimiser.probe_mode4
    docker compose start optimiser
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from .probe_mode5 import (  # reuse machinery
    BASELINE_DURATION_S,
    DEFAULT_CONFIG_PATH,
    MAX_SOC_PCT,
    MIN_PV_KW,
    MIN_SOC_PCT,
    PROBE_DURATION_S,
    RECOVERY_DURATION_S,
    Sample,
    _sample_loop,
    _summarise,
)
from .clients.sigenergy import (
    REG_ESS_MAX_CHARGING_LIMIT,
    REG_ESS_MAX_DISCHARGING_LIMIT,
    REG_GRID_EXPORT_LIMIT,
    REG_REMOTE_EMS_CONTROL_MODE,
    REG_REMOTE_EMS_ENABLE,
    SigenergyController,
)
from .config import load_config
from .types import RemoteEMSControlMode

logger = logging.getLogger("probe_mode4")

# Probe target state. Differences from probe_mode5:
#   mode: 4 (CHARGE_PV_FIRST) instead of 5
#   charge_cap: 13 kW (max_dc_charge_kw) — the key question is whether
#               this is treated as a ceiling (Option A) or a target (Option B).
#   discharge_cap: 0 — irrelevant in charge mode but keep tidy
PROBE_MODE = RemoteEMSControlMode.COMMAND_CHARGING_PV_FIRST  # = 4
PROBE_CHARGE_CAP_KW = 13.0
PROBE_EXPORT_CAP_KW = 5.0


async def _write_probe_state(controller: SigenergyController) -> bool:
    logger.warning(
        "→ writing PROBE state: mode=%s, charge_cap=%skW, exp_cap=%skW",
        PROBE_MODE.name, PROBE_CHARGE_CAP_KW, PROBE_EXPORT_CAP_KW,
    )
    ok = True
    ok &= await controller._write_u32(
        REG_ESS_MAX_CHARGING_LIMIT, int(PROBE_CHARGE_CAP_KW * 1000)
    )
    ok &= await controller._write_u32(REG_ESS_MAX_DISCHARGING_LIMIT, 0)
    ok &= await controller._write_u32(
        REG_GRID_EXPORT_LIMIT, int(PROBE_EXPORT_CAP_KW * 1000)
    )
    ok &= await controller._write_u16(REG_REMOTE_EMS_CONTROL_MODE, PROBE_MODE.value)
    return ok


async def _write_safe_state(controller: SigenergyController) -> bool:
    logger.warning("→ writing SAFE state: mode=MAXIMUM_SELF_CONSUMPTION, exp_cap=5kW")
    ok = True
    ok &= await controller._write_u16(
        REG_REMOTE_EMS_CONTROL_MODE,
        RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION.value,
    )
    ok &= await controller._write_u32(REG_GRID_EXPORT_LIMIT, 5000)
    ok &= await controller._write_u32(REG_ESS_MAX_CHARGING_LIMIT, 0)
    ok &= await controller._write_u32(REG_ESS_MAX_DISCHARGING_LIMIT, 0)
    return ok


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
        if not await controller._read_input_u16(REG_REMOTE_EMS_ENABLE):
            if not await controller.enable_remote_ems():
                logger.error("Could not enable Remote EMS — aborting.")
                return 3
        controller._remote_ems_enabled = True

        preflight = await controller.read_state()
        if preflight is None:
            return 4
        pv = preflight.pv_power_kw or 0.0
        soc = preflight.soc_pct or 0.0
        logger.info("Pre-flight: PV=%.2fkW SOC=%.1f%% load=%.2fkW grid=%.2fkW",
                    pv, soc, preflight.house_load_kw or 0.0,
                    preflight.grid_power_kw or 0.0)
        if pv < MIN_PV_KW:
            logger.error("PV (%.2f kW) < %.1f kW — no surplus to observe.", pv, MIN_PV_KW)
            return 5
        if not (MIN_SOC_PCT <= soc <= MAX_SOC_PCT):
            logger.error("SOC (%.1f%%) outside [%.0f, %.0f]%% window. Aborting.",
                         soc, MIN_SOC_PCT, MAX_SOC_PCT)
            return 6

        t0 = time.monotonic()

        logger.info("Phase 1/3: BASELINE (%ds)", BASELINE_DURATION_S)
        await _sample_loop(controller, "baseline", BASELINE_DURATION_S, samples, t0)

        logger.info("Phase 2/3: PROBE (%ds, mode 4, cap=%skW)",
                    PROBE_DURATION_S, PROBE_CHARGE_CAP_KW)
        if not await _write_probe_state(controller):
            logger.error("Could not write probe state — aborting before sampling.")
            return 7
        probe_started = True
        await _sample_loop(controller, "probe", PROBE_DURATION_S, samples, t0)

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
            try:
                await _write_safe_state(controller)
            except Exception:
                logger.exception("Safe-state revert FAILED — relying on watchdog.")
        import json
        from dataclasses import asdict
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
    p = argparse.ArgumentParser(description="Sigenergy mode-4 surplus-PV probe.")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), type=Path)
    p.add_argument(
        "--dump",
        default="/var/lib/energy-optimiser/probe_mode4.ndjson",
        type=lambda s: Path(s) if s else None,
    )
    args = p.parse_args()
    return asyncio.run(run(args.config, args.dump))


if __name__ == "__main__":
    sys.exit(main())
