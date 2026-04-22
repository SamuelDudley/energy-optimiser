"""Entry point for the energy optimiser service.

Usage:
    python -m optimiser --config /path/to/config.toml
"""

from __future__ import annotations

import argparse
import asyncio
import signal

from .config import load_config
from .logging_utils import setup_logging
from .service import Service


async def _async_main(config_path: str) -> None:
    # Service construction must happen inside the event loop: pymodbus 3.13's
    # AsyncModbusTcpClient calls asyncio.get_running_loop() in __init__.
    config = load_config(config_path)
    service = Service(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(service.stop()))

    try:
        await service.start()
    except KeyboardInterrupt:
        await service.stop()


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(description="Energy optimiser")
    parser.add_argument(
        "--config",
        "-c",
        default="/etc/energy-optimiser/config.toml",
        help="Path to TOML config file",
    )
    args = parser.parse_args()

    asyncio.run(_async_main(args.config))


if __name__ == "__main__":
    main()
