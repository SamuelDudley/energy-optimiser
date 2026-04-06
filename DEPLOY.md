# First Deployment Guide

## Pre-flight

### Hardware inventory

| Component | Model | Interface | Address |
|---|---|---|---|
| Inverter | Sigenergy SigenStor EC | Modbus TCP port 502 | `<inverter_ip>` |
| Battery | Sigenergy 40kWh (4×10kWh) | Via inverter | slave 247 (plant) |
| Solar | ~13kW array, 4 MPPT | Via inverter | — |
| HW heat pump | Haier HP330M1-U1 | Via Shelly relay | — |
| CT clamp (HW) | Shelly Pro EM | HTTP RPC | `<shelly_ip>` |
| CT clamp (mains) | Shelly Pro EM | HTTP RPC | `<shelly_mains_ip>` |
| Router | UniFi UDM/UDM-Pro | HTTPS API | `<unifi_ip>` |

### API keys needed

- **Amber Electric:** API key from https://app.amber.com.au/developers + site ID from `/sites` endpoint
- **Solcast:** API key from https://toolkit.solcast.com.au + rooftop resource ID

### Network

The service needs LAN access to:
- Inverter Modbus TCP (port 502)
- Shelly HTTP RPC (port 80)
- UniFi controller HTTPS (port 443)

And internet access to:
- `api.amber.com.au` (HTTPS)
- `api.solcast.com.au` (HTTPS)
- `reg.bom.gov.au` (HTTP)

Docker must run in host network mode (no NAT/bridge).


## Config

Create `/etc/energy-optimiser/config.toml`:

```toml
[amber]
api_key = "YOUR_AMBER_API_KEY"
site_id = "YOUR_AMBER_SITE_ID"
poll_5min_interval_s = 60
poll_30min_interval_s = 300
forecast_intervals_30min = 72    # Up to ~36h — Amber returns what they have

[solcast]
enabled = true
api_key = "YOUR_SOLCAST_API_KEY"
resource_id = "YOUR_SOLCAST_RESOURCE_ID"
max_calls_per_day = 10

[sigenergy]
host = "INVERTER_IP"
port = 502
slave_id = 247          # Plant address — reads aggregate plant data

[battery]
capacity_kwh = 40.0
max_ac_charge_kw = 10.0
max_dc_charge_kw = 13.0
max_discharge_kw = 10.0
round_trip_efficiency = 0.90
soc_floor_pct = 10.0
soc_ceiling_pct = 95.0

[weather]
bom_url = "http://reg.bom.gov.au/fwo/IDN60903/IDN60903.94926.json"
poll_interval_s = 1800

[occupancy]
controller_url = "https://UNIFI_IP"
username = "YOUR_UNIFI_USER"
password = "YOUR_UNIFI_PASSWORD"
site = "default"
poll_interval_s = 300
phone_macs = ["aa:bb:cc:dd:ee:ff"]   # Your phone MAC(s)
grace_period_min = 30

[storage]
db_path = "/var/lib/energy-optimiser/telemetry.duckdb"
snapshot_dir = "/var/lib/energy-optimiser/snapshots"

[planner]
tick_interval_s = 60
telemetry_write_interval_s = 300
lp_wall_clock_timeout_s = 12.0
lp_scenario_weight_p10 = 0.20
lp_scenario_weight_p50 = 0.60
lp_scenario_weight_p90 = 0.20

# Optional: HW heat pump as managed load
# Comment out entirely to run battery-only
# [[managed_load]]
# load_id = "hot_water"
# category = "signal_driven"
# shelly_host = "SHELLY_HW_IP"
# shelly_channel = 0
# has_relay = true
# daily_target_kwh = 4.0
# draw_kw = 0.9
# deadline_hour_local = 22
```


## Docker Compose

```yaml
version: "3.8"
services:
  energy-optimiser:
    build: .
    network_mode: host
    restart: unless-stopped
    volumes:
      - /etc/energy-optimiser:/etc/energy-optimiser:ro
      - /var/lib/energy-optimiser:/var/lib/energy-optimiser
    environment:
      - EO_CONFIG=/etc/energy-optimiser/config.toml
    healthcheck:
      test: ["CMD", "python", "-c", "import sys; sys.exit(0)"]
      interval: 60s
      timeout: 10s
      retries: 3
```


## First-run verification (phased)

### Phase 0: Standalone Modbus read (before starting the service)

**Goal:** verify register addressing against live hardware without starting
the full service. Run from the Docker host or any machine on the same LAN.

```bash
python3 -c "
import asyncio
from pymodbus.client import AsyncModbusTcpClient

async def verify():
    c = AsyncModbusTcpClient('INVERTER_IP', port=502)
    await c.connect()

    # SOC (30014, U16, gain=10)
    r = await c.read_input_registers(address=30014, count=1, slave=247)
    soc = r.registers[0] / 10
    print(f'SOC: {soc:.1f}%')

    # Grid sensor active power (30005, S32, gain=1000)
    r = await c.read_input_registers(address=30005, count=2, slave=247)
    raw = (r.registers[0] << 16) | r.registers[1]
    if raw >= 0x80000000: raw -= 0x100000000
    print(f'Grid: {raw/1000:.2f} kW (>0 importing, <0 exporting)')

    # PV power (30035, S32, gain=1000)
    r = await c.read_input_registers(address=30035, count=2, slave=247)
    raw = (r.registers[0] << 16) | r.registers[1]
    if raw >= 0x80000000: raw -= 0x100000000
    print(f'PV: {raw/1000:.2f} kW')

    # ESS power (30037, S32, gain=1000)
    r = await c.read_input_registers(address=30037, count=2, slave=247)
    raw = (r.registers[0] << 16) | r.registers[1]
    if raw >= 0x80000000: raw -= 0x100000000
    print(f'Battery: {raw/1000:+.2f} kW (+ charging, - discharging)')

    # EMS mode (30003, U16) and grid sensor (30004, U16)
    r = await c.read_input_registers(address=30003, count=2, slave=247)
    print(f'EMS mode: {r.registers[0]} (0=self, 1=AI, 2=TOU, 7=remote)')
    print(f'Grid sensor: {\"connected\" if r.registers[1] == 1 else \"NOT connected\"}')

    c.close()
    print()
    print('Compare all values against the Sigenergy app.')
    print('If SOC matches within 1%: register addressing confirmed.')

asyncio.run(verify())
"
```

**Stop here if SOC doesn't match the app.** Everything downstream depends
on correct register addressing. If it's wrong, check pymodbus version and
the Sigenergy HA integration for the correct offset pattern.

### Phase 1: Read-only (no writes)

**Goal:** verify Modbus reads work, APIs connect, data flows.

1. Start the container with the managed_load section commented out
2. Watch logs: `docker logs -f energy-optimiser`
3. Verify in logs:
   - `"Remote EMS enabled"` — confirms register 40029 write worked
   - `"State: initialise → active"` — both Modbus and Amber connected
   - `soc` values in TICK_COMPLETE events match the Sigenergy app
   - `pv_kw` and `grid_kw` values are sensible (not 0, not millions)
   - Solcast forecasts arrive (`solcast` wake loop logs)
   - BOM temperature is reasonable

4. **Critical register check:** read the app's SOC display. Compare to the
   `soc` field in the log. If they match → register addressing is correct.
   If they differ → stop immediately and investigate (likely addressing offset).

5. Let it run for 1 hour in this mode. Check the snapshot files:
   ```bash
   ls -la /var/lib/energy-optimiser/snapshots/
   zcat /var/lib/energy-optimiser/snapshots/$(date +%Y-%m-%d)*.ndjson.gz | head -1 | python -m json.tool | head -20
   ```

### Phase 2: Battery control (LP active)

**Goal:** verify the LP's dispatch is being applied and the inverter follows.

1. The LP is already running from Phase 1 — check TICK_COMPLETE events
   for `"action"` values other than `"self_consume"`:
   - During cheap overnight periods: expect `"charge_grid"`
   - During expensive evenings: expect `"discharge_ess"`
   - During PV surplus: expect `"charge_pv"`

2. Check MODBUS_WRITE events in logs — they show exactly which registers
   are being written and what values.

3. Cross-reference with Sigenergy app:
   - When the log says `"Applied LP dispatch: mode=COMMAND_CHARGING_GRID_FIRST cap=5.00kW"`,
     the app should show the battery charging
   - Battery power direction should match the LP's intent

4. Watch for VERIFY_DEVIATION events — these mean the watcher detected
   the inverter not following the commanded dispatch. Occasional single
   deviations are normal (measurement noise). Three consecutive = fallback.

5. Let it run for 24 hours. Check the telemetry database:
   ```bash
   docker exec energy-optimiser python -c "
   import duckdb
   db = duckdb.connect('/var/lib/energy-optimiser/telemetry.duckdb', read_only=True)
   print(db.sql('SELECT COUNT(*), MIN(ts), MAX(ts) FROM telemetry').fetchall())
   print(db.sql(\"\"\"
     SELECT planner_action, COUNT(*) as n, AVG(import_price) as avg_price
     FROM telemetry WHERE planner_action IS NOT NULL
     GROUP BY planner_action ORDER BY n DESC
   \"\"\").fetchall())
   "
   ```

### Phase 3: Add managed loads (optional)

1. Uncomment the `[[managed_load]]` section in config
2. Restart the container
3. Verify Shelly connectivity in logs
4. Watch for LOAD_CYCLE_START/COMPLETE events
5. Check the heat pump is actually responding to relay commands


## Rollback

If anything goes wrong:

```bash
docker stop energy-optimiser
```

The inverter reverts to its local EMS mode automatically when Modbus
communication stops (or within the configured heartbeat timeout if set).
The `set_fallback()` call during service shutdown sets mode 2
(Maximum Self Consumption) explicitly.

If the container crashes without clean shutdown, the inverter will
still revert — the Sigenergy firmware has a built-in "no Modbus
communication for N seconds → revert to local mode" safety. Check
the Sigenergy app to confirm the inverter has reverted.


## Monitoring

Key events to watch for (grep these from Docker logs):

| Event | Meaning | Action |
|---|---|---|
| `FALLBACK_TRIGGERED` | LP failed or inverter not following | Check reason field; usually transient |
| `BREAKER_LATCHED` | 3 consecutive verify deviations | Inverter may be misbehaving; check app |
| `MODBUS_ERROR` | Failed to read/write inverter | Network issue or inverter restart |
| `VERIFY_DEVIATION` | Single poll mismatch | Normal if isolated; concerning if repeated |
| `LP_SOLVE_COMPLETE` | Each LP solve result | Check `solve_ms` stays under 5000 |


## Expected costs/savings

With Amber wholesale + 40kWh battery + 13kW solar in Canberra:
- **Battery arbitrage:** $800-1200/year (charge cheap overnight, discharge expensive evening)
- **PV self-consumption optimisation:** $200-400/year (store midday surplus for evening)
- **HW heat pump scheduling:** $100-200/year (run during cheap/PV periods)
- **Export cap management:** $50-100/year (avoid curtailment)

Total estimated: **$1200-1900/year** savings vs the inverter's built-in AI mode.
These are estimates based on 2025 Amber pricing in Canberra; actual results
depend on your specific consumption pattern and market conditions.
