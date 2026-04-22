# First Deployment Guide

## Pre-flight

### Hardware inventory

| Component | Model | Interface | Address |
|---|---|---|---|
| Inverter | Sigenergy SigenStor EC | Modbus TCP port 502 | `<inverter_ip>` |
| Battery | Sigenergy 40 kWh (4×10 kWh) | Via inverter | slave 247 (plant) |
| Solar | ~13 kW array, 4 MPPT | Via inverter | — |
| HW heat pump (optional) | Haier HP330M1-U1 | Via Shelly relay | — |
| CT clamp (optional) | Shelly Pro EM | HTTP RPC | `<shelly_ip>` |
| Router (optional) | UniFi UDM/UDM-Pro | HTTPS API | `<unifi_ip>` |

### API keys needed

- **Amber Electric:** API key from https://app.amber.com.au/developers + site ID from `/sites` endpoint
- **Solcast:** API key from https://toolkit.solcast.com.au + rooftop resource ID (hard 10 calls/day quota on the hobbyist tier)

### Network

LAN access: Inverter Modbus TCP 502, Shelly HTTP 80, UniFi HTTPS 443.
Internet: `api.amber.com.au`, `api.solcast.com.au`, `reg.bom.gov.au`.
Docker runs in host network mode (no NAT/bridge).


## Config

Copy `config.example.toml` to `config.toml` in the repo root and fill in
secrets. The example is the authoritative template — do not hand-edit a
schema from DEPLOY.md, it will drift. Minimum required fields:

- `[amber] api_key`, `site_id`
- `[solcast] api_key`, `resource_id` (or set `enabled = false`)
- `[sigenergy] host`, `slave_id = 247`

`config.toml` is gitignored — it contains live API keys.


## First-run verification (phased)

Run each phase, confirm clean output, then proceed. Stop and investigate
if anything surprises you — actuations start at Phase 3.

### Phase 0 — Offline LP sanity

Pure in-memory solve, no network, no Modbus. Confirms the image builds
and the LP+HiGHS solver works.

```bash
docker compose build
docker run --rm -v $PWD/config.toml:/etc/energy-optimiser/config.toml:ro \
  --entrypoint eo-smoke \
  energy-optimiser-optimiser \
  -c /etc/energy-optimiser/config.toml --offline
```

### Phase 1 — Read-only Modbus probe

Reads SOC, grid, battery, PV, EMS mode. **Compare SOC to the Sigenergy
app's display within ~1%.** If it doesn't match, stop — register
addressing or pymodbus API has shifted.

```bash
docker run --rm --network host \
  -v $PWD/config.toml:/etc/energy-optimiser/config.toml:ro \
  --entrypoint eo-smoke \
  energy-optimiser-optimiser \
  -c /etc/energy-optimiser/config.toml --modbus-read
```

### Phase 2 — API probes

One call each to Amber, Solcast, BOM, UniFi. Solcast spends 1 of the
10/day quota; skip if you want to reserve it for the running service.

```bash
docker run --rm --network host \
  -v $PWD/config.toml:/etc/energy-optimiser/config.toml:ro \
  --entrypoint eo-smoke \
  energy-optimiser-optimiser \
  -c /etc/energy-optimiser/config.toml --api-probe
```

### Phase 3 — Dry tick (full pipeline, no writes)

Runs the real tick pipeline — LP solve, dispatch decision, snapshot
write — but *prints* what would be written to the inverter instead
of actuating. Last checkpoint before the real service.

```bash
docker run --rm --network host \
  -v $PWD/config.toml:/etc/energy-optimiser/config.toml:ro \
  -v eo-smoke-data:/var/lib/energy-optimiser \
  --entrypoint eo-smoke \
  energy-optimiser-optimiser \
  -c /etc/energy-optimiser/config.toml --dry-tick
```

### Phase 4 — Bring up the service

```bash
docker compose up -d
docker compose logs -f
```

This starts two containers: `energy-optimiser` (the main service) and
`energy-optimiser-watchdog` (the dead-man sidecar). See
`docker-compose.yml` for the wiring — network, volumes, env, restart
policies are already set.

**First 5 minutes — verify hand-over is sane:**
- `"Remote EMS enabled"` — the reg 40029 write landed
- `"State: initialise → active"` — Modbus + Amber both healthy
- A `TICK_COMPLETE` event with an `action` and `soc_pct` value
- **SOC in the log matches the Sigenergy app within ~1%** (the real closure of the register-addressing question)

**Watchdog sanity** — the sidecar's startup line:
```
eo-watchdog starting: heartbeat=/var/lib/energy-optimiser/heartbeat host=... stale_after=90s poll=15s grace=180s
```
If it ever emits `FALLBACK FIRED`, the main service went silent and the
watchdog pinned the inverter to `(mode=2, export=0, remote_ems=1)` — or
fell through to `remote_ems=0` if any write failed. Investigate — see
KNOWN-ISSUES #0d for what that means.

### Phase 5 — Let it run, then verify telemetry

After 1–24 hours, query the telemetry DB via the **snapshot-and-query**
pattern. The running service holds a write lock on `telemetry.duckdb`;
DuckDB doesn't allow a second connection even for `read_only=True`. Copy
the file aside inside the container and query the copy:

```bash
docker exec energy-optimiser bash -c '
  cp /var/lib/energy-optimiser/telemetry.duckdb /tmp/tel.duckdb
  python -c "
import duckdb
db = duckdb.connect(\"/tmp/tel.duckdb\", read_only=True)
print(db.sql(\"SELECT COUNT(*), MIN(ts), MAX(ts) FROM telemetry\").fetchall())
for row in db.sql(\"\"\"
  SELECT planner_action, COUNT(*) AS n, AVG(import_price) AS avg_price
  FROM telemetry WHERE planner_action IS NOT NULL
  GROUP BY planner_action ORDER BY n DESC
\"\"\").fetchall():
    print(row)
"
'
```

The snapshot is a point-in-time copy — stale up to the next telemetry
write (5 min). For live state, use `docker compose logs -f` or read the
latest NDJSON snapshot under `snapshots/`.

### Phase 6 — Install the systemd unit (boot-time autostart)

```bash
sudo install -m 644 energy-optimiser.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable energy-optimiser.service
sudo systemctl start energy-optimiser.service
```

Verify:
```bash
systemctl is-enabled energy-optimiser.service   # "enabled"
systemctl status energy-optimiser.service       # "active (exited)"
```

The unit runs `docker compose up -d` as user `dudley` (docker group) at
boot after `docker.service`. `RemainAfterExit=yes` keeps it "active"
for systemd's bookkeeping; Docker actually owns the container
lifecycle. On shutdown, `docker compose down` runs — the service then
writes `set_fallback()` explicitly before exiting.

### Phase 7 — Add managed loads (optional, Shelly hardware required)

1. Uncomment the `[[managed_load]]` section in `config.toml`
2. `docker compose restart optimiser`
3. Verify Shelly connectivity in logs
4. Watch for `LOAD_CYCLE_START` / `LOAD_CYCLE_COMPLETE` events


## Rollback

Clean rollback:
```bash
docker compose stop         # SIGTERM → set_fallback() → mode 2 written explicitly
```

Or via systemd:
```bash
sudo systemctl stop energy-optimiser.service
```

**Ungraceful crash (SIGKILL, OOM, kernel panic):** `set_fallback()`
doesn't run. The Sigenergy firmware has **no** Modbus communication
watchdog (verified on live hardware 2026-04-22 — see KNOWN-ISSUES #0d),
so the inverter holds the last commanded mode indefinitely. The
dead-man watchdog sidecar catches this: if the main service stops
touching its heartbeat file (`/var/lib/energy-optimiser/heartbeat`) for
>90 s, the watchdog pins the inverter explicitly — three writes:
`40031=2` (MAX_SELF_CONSUMPTION), `40038=0` (no export), `40029=1`
(REMOTE_EMS_ENABLE). If any of those fails, last-resort `40029=0`
hands control to the inverter's local EMS config. Writes re-assert on
every poll while stale.

If both containers are gone (host down, docker daemon crash), you'll
need to intervene manually — use the Sigenergy app to confirm the
plant mode, or write `40029=0` from any Modbus-capable tool on the LAN.


## Monitoring

Events to grep from `docker compose logs`:

| Event | Source | Meaning | Action |
|---|---|---|---|
| `FALLBACK_TRIGGERED` | service | LP failed or inverter not following | Check reason; usually transient |
| `BREAKER_LATCHED` | service | 3 consecutive verify deviations | Inverter may be misbehaving; check app |
| `MODBUS_ERROR` | service | Failed to read/write inverter | Network issue or inverter restart |
| `VERIFY_DEVIATION` | service | Single poll mismatch | Normal if isolated; concerning if repeated |
| `LP_SOLVE_COMPLETE` | service | Each LP solve | Confirm `solve_ms < 5000` |
| `FALLBACK FIRED` | watchdog | Heartbeat went stale | Main service died — check its logs for root cause |
| `heartbeat recovered` | watchdog | Main service re-started ticking | No action, watchdog re-arms |


## Expected savings

With Amber wholesale + 40 kWh battery + 13 kW solar in Canberra (rough
order-of-magnitude estimates from design-time modelling; actual results
depend on consumption pattern and market):

- Battery arbitrage (charge cheap overnight, discharge expensive evening): $800–1200/yr
- PV self-consumption shifting (store midday surplus for evening): $200–400/yr
- HW heat pump scheduling (run during cheap/PV periods): $100–200/yr
- Export cap management (avoid curtailment): $50–100/yr

Total: **$1200–1900/yr** vs the inverter's built-in AI mode.
