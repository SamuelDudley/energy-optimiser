"""External dead-man watchdog for the energy optimiser.

Runs as a separate container with a restricted dependency surface (stdlib +
pymodbus only — no duckdb, no HiGHS, no httpx). Its sole job: if the main
service stops updating a heartbeat file for too long, drive the inverter
into an explicit known-safe state via Modbus.

Why this exists: the Sigenergy firmware has no Modbus communication
watchdog (verified 2026-04-22 — see KNOWN-ISSUES #0d). A crash of the
main service without a clean SIGTERM leaves the inverter executing the
last commanded mode indefinitely. Docker `restart: unless-stopped` covers
Python-level crashes (container supervisor restarts the service), but
not: OOM kill, container-runtime death, kernel panic, Python deadlock
with the interpreter alive but not ticking.

Fallback protocol (three explicit writes, in order):
  1. 40031 = 2   — RemoteEMSControlMode = MAXIMUM_SELF_CONSUMPTION
  2. 40038 = 0   — grid export limit = 0 kW
  3. 40029 = 1   — REMOTE_EMS_ENABLE = 1 (so the mode set in step 1 takes
                  effect; the explicit state is pinned rather than falling
                  back to whatever the Sigenergy app had configured locally)

If any of those three writes fails, the watchdog falls through to a
last-resort single write: 40029 = 0, which disables remote EMS and hands
control back to the inverter's local EMS config. Less deterministic than
the explicit pin, but requires only one successful write and is idempotent.

Re-assertion while stale: the fallback writes run on every poll while the
heartbeat is stale, not just once. Each write is idempotent, and
re-asserting defends against transient Modbus drops between the first
fire and service recovery.

The watchdog is deliberately small and independent so it can survive
conditions that take the main service down:
  - No shared config file — sigenergy connection params via env/args
  - No project imports beyond pymodbus
  - No async event loop on the hot path (except pymodbus's own)
  - No log rotation, no DuckDB — writes to stderr only
  - All three writes are idempotent; the last-resort write is too
  - Stateless: can restart mid-fire and continue correctly

Protocol: the main service calls `heartbeat_touch(path)` (see
logging_utils or service) at the end of every successful tick. The
watchdog stats the file every `poll_seconds`; if mtime is older than
`stale_seconds`, it drives the fallback writes and emits a loud message.
It keeps running after firing — if the service restarts and crashes
again, we want to re-fire.
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
REG_GRID_EXPORT_POWER_LIMIT = 40038  # U16: kW * 1000 (0 = no export)

MODE_MAXIMUM_SELF_CONSUMPTION = 2  # RemoteEMSControlMode enum value

# Exit codes — the container is configured `restart: unless-stopped`, so
# any non-zero exit triggers a restart. The watchdog itself should never
# exit voluntarily during normal operation.
EXIT_CONFIG = 2
EXIT_UNEXPECTED = 3

logger = logging.getLogger("eo-watchdog")


WRITE_MAX_ATTEMPTS = 2
WRITE_RETRY_BACKOFF_S = 0.2


async def _write_register(
    client: AsyncModbusTcpClient,
    slave_id: int,
    address: int,
    value: int,
    label: str,
) -> bool:
    """One Modbus write with bounded retry on TCP-level failure.

    Retry only runs on `Exception` raised by pymodbus (socket dropped,
    connect-refused, etc.) — a protocol-level `isError()` response
    (illegal function, bad address) is deterministic and retrying
    won't help. Before the retry attempt we call `client.connect()`
    because the most common cause of transient write failure is a
    dead TCP socket, and pymodbus's auto-reconnect isn't guaranteed
    to fire before the next call.
    """
    for attempt in range(1, WRITE_MAX_ATTEMPTS + 1):
        try:
            result = await client.write_register(
                address=address, value=value, device_id=slave_id
            )
        except Exception as exc:
            logger.warning(
                "write %s (%d=%d) attempt %d/%d raised: %s",
                label, address, value, attempt, WRITE_MAX_ATTEMPTS, exc,
            )
            if attempt < WRITE_MAX_ATTEMPTS:
                # Reconnect + back off before the retry.
                try:
                    connect_ok = await client.connect()
                    if not connect_ok:
                        logger.warning(
                            "reconnect before retry of %s returned False", label,
                        )
                except Exception as reconnect_exc:
                    logger.warning(
                        "reconnect before retry of %s raised: %s",
                        label, reconnect_exc,
                    )
                await asyncio.sleep(WRITE_RETRY_BACKOFF_S)
                continue
            logger.error(
                "write %s (%d=%d) failed after %d attempts: last exc=%s",
                label, address, value, WRITE_MAX_ATTEMPTS, exc,
            )
            return False
        if result.isError():
            logger.error(
                "write %s (%d=%d) returned error: %s",
                label, address, value, result,
            )
            return False
        if attempt > 1:
            logger.info(
                "write %s (%d=%d) succeeded on attempt %d/%d",
                label, address, value, attempt, WRITE_MAX_ATTEMPTS,
            )
        return True
    return False  # unreachable (loop always returns); silences type checkers


async def _trigger_fallback(
    client: AsyncModbusTcpClient, slave_id: int
) -> bool:
    """Drive the inverter to an explicit known-safe state.

    Three writes in order: mode=MAXIMUM_SELF_CONSUMPTION, export_limit=0,
    remote_ems_enable=1. If any of those fails, fall through to a
    last-resort single write (remote_ems_enable=0) that hands control back
    to the inverter's local EMS config.

    Returns True iff the inverter is in a safe state afterwards — either
    the explicit pin succeeded in full, or the last-resort disable
    succeeded. Returns False only if every write failed.
    """
    ok_mode = await _write_register(
        client,
        slave_id,
        REG_REMOTE_EMS_CONTROL_MODE,
        MODE_MAXIMUM_SELF_CONSUMPTION,
        "mode=MAXIMUM_SELF_CONSUMPTION",
    )
    ok_export = await _write_register(
        client,
        slave_id,
        REG_GRID_EXPORT_POWER_LIMIT,
        0,
        "export_limit=0kW",
    )
    ok_enable = await _write_register(
        client,
        slave_id,
        REG_REMOTE_EMS_ENABLE,
        1,
        "REMOTE_EMS_ENABLE=1",
    )

    if ok_mode and ok_export and ok_enable:
        logger.warning(
            "FALLBACK FIRED — explicit self-consume pin (mode=2, export=0, "
            "remote_ems=1) on slave %d",
            slave_id,
        )
        return True

    # Partial or total failure of the explicit path. Try the last-resort
    # single-write disable, which hands control to local EMS.
    logger.error(
        "explicit fallback partial failure (mode_ok=%s export_ok=%s enable_ok=%s) — "
        "trying last-resort REMOTE_EMS_ENABLE=0",
        ok_mode,
        ok_export,
        ok_enable,
    )
    ok_last_resort = await _write_register(
        client,
        slave_id,
        REG_REMOTE_EMS_ENABLE,
        0,
        "REMOTE_EMS_ENABLE=0 (last-resort)",
    )
    if ok_last_resort:
        logger.warning(
            "FALLBACK FIRED (last-resort) — wrote REMOTE_EMS_ENABLE=0 on slave %d; "
            "inverter now following local EMS config",
            slave_id,
        )
        return True

    logger.error(
        "FALLBACK FAILED — unable to write any register on slave %d", slave_id
    )
    return False


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
    # Pre-connect so the first fallback write isn't paying the
    # connect-on-demand latency — observed ~60 s of "FALLBACK FAILED"
    # logs during a long-downtime test before pymodbus caught up. If
    # pre-connect fails (inverter unreachable at startup), we keep
    # running anyway: `_write_register` re-attempts the connect on
    # retry, and the main service may come up and freshen the
    # heartbeat before any fallback is needed.
    try:
        connect_ok = await client.connect()
        if connect_ok:
            logger.info(
                "Modbus pre-connect OK: %s:%d", sigenergy_host, sigenergy_port
            )
        else:
            logger.warning(
                "Modbus pre-connect returned False — will retry per-write "
                "on first fallback attempt",
            )
    except Exception as exc:
        logger.warning(
            "Modbus pre-connect raised: %s — will retry per-write", exc,
        )

    startup_time = time.time()
    was_stale = False  # log transitions at WARNING, re-assertions at DEBUG

    while True:
        age = _heartbeat_age_s(heartbeat_path)

        # Decide whether we're currently stale. "stale" here means
        # "something is wrong, fire the fallback this poll".
        if age is None:
            elapsed = time.time() - startup_time
            if elapsed < grace_seconds:
                logger.debug(
                    "heartbeat not yet present (%.0fs into grace)", elapsed
                )
                stale = False
            else:
                if not was_stale:
                    logger.warning(
                        "heartbeat file missing after %.0fs grace — firing fallback",
                        elapsed,
                    )
                else:
                    logger.debug(
                        "heartbeat still missing (%.0fs) — re-asserting", elapsed
                    )
                stale = True
        elif age > stale_seconds:
            if not was_stale:
                logger.warning(
                    "heartbeat stale (%.1fs > %.0fs) — firing fallback",
                    age,
                    stale_seconds,
                )
            else:
                logger.debug("still stale (%.1fs) — re-asserting fallback", age)
            stale = True
        else:
            if was_stale:
                logger.info(
                    "heartbeat recovered (%.1fs old) — re-arming", age
                )
            stale = False

        if stale:
            # Re-assert every poll. Writes are idempotent; re-assertion
            # defends against transient Modbus drops between the first
            # fire and service recovery.
            await _trigger_fallback(client, slave_id)

        was_stale = stale
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
