# Energy Optimiser

Amber wholesale energy optimiser for a Sigenergy hybrid inverter with ~13 kW
solar and a 40 kWh battery. Solves a stochastic MILP (PuLP + HiGHS) over
5-min slots across the priced Amber forecast horizon, applies the slot-0
decision to the inverter via Modbus, and verifies behaviour against a
10-second watchdog.

Co-optimises battery dispatch with a Shelly-relayed Haier HP330M1-U1 hot
water heat pump in PV mode.

## Requirements

- Python 3.12 (the project pins this in `.python-version`)
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for dependency management
- Optional: Docker + Docker Compose for production deploy

## Quick start

```bash
uv sync                       # install the project + dev group
uv run pytest tests/ -q       # full test suite — expect 213 passed
```

## Local smoke testing (before touching hardware)

```bash
cp config.example.toml config.toml         # fill in secrets
uv run eo-smoke -c config.toml --offline   # synthetic LP solve, no network
```

## Hardware deploy sequence

Run each smoke phase against live hardware and confirm clean output before
moving to the next:

```bash
uv run eo-smoke -c config.toml --offline       # zero risk
uv run eo-smoke -c config.toml --modbus-read   # read-only Modbus probe
uv run eo-smoke -c config.toml --api-probe     # 1 call per API (costs 1 Solcast quota)
uv run eo-smoke -c config.toml --dry-tick      # full tick, prints proposed writes, no actuations
```

Once all four are clean, start the service:

```bash
uv run energy-optimiser --config config.toml
```

Or with Docker Compose:

```bash
docker compose up -d
```

## Replay (post-deploy, once snapshots are flowing)

```bash
uv run eo-replay \
    -s '/var/lib/energy-optimiser/snapshots/2026-*.ndjson.gz' \
    -c candidate-config.toml \
    -o results.ndjson -v
```

Compares a candidate LP configuration against historical snapshot ticks.

## Entry points

- `energy-optimiser` — the service (tick loop + wake loops)
- `eo-smoke` — read-only pre-deploy smoke tests
- `eo-replay` — replay historical ticks against a candidate config

## Documentation

- `CLAUDE.md` — development guide (architecture, conventions, testing)
- `SPEC-ENERGY-01.md` — authoritative spec (design decisions, register map, acceptance criteria)
- `DEPLOY.md` — first-deployment runbook
- `KNOWN-ISSUES.md` — open issues and resolution history
