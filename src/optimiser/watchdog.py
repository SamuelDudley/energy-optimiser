"""External dead-man watchdog for the energy optimiser.

Runs as a separate container with a restricted dependency surface (stdlib +
pymodbus only — no duckdb, no HiGHS, no httpx). Its sole job: if the main
service stops updating a heartbeat file for too long, write
REMOTE_EMS_ENABLE (40029) = 0 to the inverter so the plant reverts to its
local EMS mode.

Why this exists: the Sigenergy firmware has no Modbus communication
watchdog (verified 2026-04-22 — see KNOWN-ISSUES #0d). A crash of the
main service without a clean SIGTERM leaves the inverter executing the
last commanded mode indefinitely. Docker `restart: unless-stopped` covers
Python-level crashes (container supervisor restarts the service), but
not: OOM kill, container-runtime death, kernel panic, Python deadlock
with the interpreter alive but not ticking.

The watchdog is deliberately small and independent so it can survive
conditions that take the main service down:
  - No shared config file — sigenergy connection params via env/args
  - No project imports beyond pymodbus
  - No async event loop on the hot path (except pymodbus's own)
  - No log rotation, no DuckDB — writes to stderr only
  - Idempotent: writing 40029 = 0 repeatedly is safe
  - Stateless: can restart mid-fire and continue correctly

Protocol: the main service calls `heartbeat_touch(path)` (see
logging_utils or service) at the end of every successful tick. The
watchdog stats the file every `poll_seconds`; if mtime is older than
`stale_seconds`, it writes to Modbus and emits a loud message. It keeps
running after firing — if the service restarts and crashes again, we
want to re-fire.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from pymodbus.client import AsyncModbusTcpClient

# Register addresses — duplicated intentionally from clients/sigenergy.py so
# the watchdog has no import dependency on the main service package. If
# these ever change, both places need updating.
REG_REMOTE_EMS_ENABLE = 40029  # U16: 0 = disabled (local EMS), 1 = enabled
REG_REMOTE_EMS_CONTROL_MODE = 40031  # U16: RemoteEMSControlMode

# Exit codes — the container is configured `restart: unless-stopped`, so
# any non-zero exit triggers a restart. The watchdog itself should never
# exit voluntarily during normal operation.
EXIT_CONFIG = 2
EXIT_UNEXPECTED = 3

logger = logging.getLogger("eo-watchdog")


async def _trigger_fallback(
    client: AsyncModbusTcpClient, slave_id: int
) -> bool:
    """Write the single-register fallback: REMOTE_EMS_ENABLE = 0.

    Once 40029 goes to 0, the plant ignores 40031 and reverts to whatever
    local EMS mode was configured via the Sigenergy app. This is the
    "safest revert" identified by the protocol audit in KNOWN-ISSUES #0d.
    Returns True on success.
    """
    try:
        result = await client.write_register(
            address=REG_REMOTE_EMS_ENABLE,
            value=0,
            device_id=slave_id,
        )
    except Exception as exc:
        logger.error("fallback write raised: %s", exc)
        return False
    if result.isError():
        logger.error("fallback write returned error: %s", result)
        return False
    logger.warning(
        "FALLBACK FIRED — wrote REMOTE_EMS_ENABLE (%d) = 0 to slave %d",
        REG_REMOTE_EMS_ENABLE,
        slave_id,
    )
    return True


def _heartbeat_age_s(path: Path) -> float | None:
    """Return the age (in seconds) of the heartbeat file, or None if the
    file doesn't exist yet. A missing file during early startup is
    treated as "not-stale-yet" rather than "stale forever" so the
    watchdog doesn't fire before the main service has had a chance to
    touch the file for the first time."""
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    return time.time() - mtime


async def run(
    *,
    heartbeat_path: Path,
    sigenergy_host: str,
    sigenergy_port: int,
    slave_id: int,
    stale_seconds: float,
    poll_seconds: float,
    grace_seconds: float,
) -> None:
    """Main loop. Does not return under normal operation."""
    logger.info(
        "eo-watchdog starting: heartbeat=%s host=%s:%d slave=%d "
        "stale_after=%.0fs poll=%.0fs grace=%.0fs",
        heartbeat_path,
        sigenergy_host,
        sigenergy_port,
        slave_id,
        stale_seconds,
        poll_seconds,
        grace_seconds,
    )

    client = AsyncModbusTcpClient(host=sigenergy_host, port=sigenergy_port)
    # We don't pre-connect; pymodbus reconnects per-write. That's fine
    # here — we only write on the failure path, and a fresh connect when
    # we actually need it avoids stale sockets after a long idle period.

    startup_time = time.time()
    fired = False  # one-shot per staleness episode — only log once per fire

    while True:
        age = _heartbeat_age_s(heartbeat_path)

        if age is None:
            # File doesn't exist. Tolerate this only during the initial
            # grace window — after that, treat missing as stale.
            elapsed = time.time() - startup_time
            if elapsed < grace_seconds:
                logger.debug(
                    "heartbeat not yet present (%.0fs into grace)", elapsed
                )
            else:
                logger.warning(
                    "heartbeat file missing after %.0fs grace — firing fallback",
                    elapsed,
                )
                if not fired:
                    await _trigger_fallback(client, slave_id)
                    fired = True
        elif age > stale_seconds:
            if not fired:
                logger.warning(
                    "heartbeat stale (%.1fs > %.0fs) — firing fallback",
                    age,
                    stale_seconds,
                )
                await _trigger_fallback(client, slave_id)
                fired = True
            else:
                logger.debug("still stale (%.1fs) — fallback already fired", age)
        else:
            if fired:
                logger.info(
                    "heartbeat recovered (%.1fs old) — re-arming", age
                )
                fired = False

        await asyncio.sleep(poll_seconds)


def _getenv_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.error("invalid %s=%r, using default %s", name, raw, default)
        return default


def _getenv_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.error("invalid %s=%r, using default %s", name, raw, default)
        return default


def main() -> None:
    # Minimal logging: stderr, ISO timestamp, severity, message. No JSON,
    # no file handler. Failure-mode readability matters more than
    # structured ingest for a component this simple.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        description="Dead-man watchdog for the energy optimiser service."
    )
    parser.add_argument(
        "--heartbeat",
        default=os.environ.get(
            "EO_WATCHDOG_HEARTBEAT", "/var/lib/energy-optimiser/heartbeat"
        ),
        help="Path to the heartbeat file the main service touches each tick.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("EO_WATCHDOG_SIGENERGY_HOST"),
        help="Sigenergy inverter host/IP (env: EO_WATCHDOG_SIGENERGY_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_getenv_int("EO_WATCHDOG_SIGENERGY_PORT", 502),
        help="Sigenergy Modbus TCP port (default 502).",
    )
    parser.add_argument(
        "--slave-id",
        type=int,
        default=_getenv_int("EO_WATCHDOG_SIGENERGY_SLAVE_ID", 247),
        help="Modbus slave/device ID (default 247, plant address).",
    )
    parser.add_argument(
        "--stale-seconds",
        type=float,
        default=_getenv_float("EO_WATCHDOG_STALE_SECONDS", 60.0),
        help=(
            "Heartbeat age (seconds) above which the watchdog fires. "
            "Default 60s. The main service ticks every 60s by default, so "
            "this allows one missed tick before triggering."
        ),
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=_getenv_float("EO_WATCHDOG_POLL_SECONDS", 15.0),
        help=(
            "How often to check the heartbeat file. Default 15s — four "
            "checks per stale window at the default stale_seconds."
        ),
    )
    parser.add_argument(
        "--grace-seconds",
        type=float,
        default=_getenv_float("EO_WATCHDOG_GRACE_SECONDS", 120.0),
        help=(
            "Tolerate a missing heartbeat file for this many seconds after "
            "startup (default 120s). Lets the main service complete its "
            "first tick — initial Modbus connect + Amber/Solcast/BOM "
            "fetches — before the watchdog concludes it's dead."
        ),
    )
    args = parser.parse_args()

    if not args.host:
        logger.error(
            "No --host given and EO_WATCHDOG_SIGENERGY_HOST is unset. "
            "Refusing to start — cannot write fallback without a target."
        )
        sys.exit(EXIT_CONFIG)

    try:
        asyncio.run(
            run(
                heartbeat_path=Path(args.heartbeat),
                sigenergy_host=args.host,
                sigenergy_port=args.port,
                slave_id=args.slave_id,
                stale_seconds=args.stale_seconds,
                poll_seconds=args.poll_seconds,
                grace_seconds=args.grace_seconds,
            )
        )
    except KeyboardInterrupt:
        logger.info("shutdown signal received")
    except Exception:
        logger.exception("unexpected error — exiting for restart")
        sys.exit(EXIT_UNEXPECTED)


if __name__ == "__main__":
    main()
