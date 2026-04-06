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

    config = load_config(args.config)
    service = Service(config)

    loop = asyncio.new_event_loop()

    # Handle shutdown signals
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(service.stop()))

    try:
        loop.run_until_complete(service.start())
    except KeyboardInterrupt:
        loop.run_until_complete(service.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
